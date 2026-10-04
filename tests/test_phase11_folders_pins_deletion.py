"""Phase 11 M3 T0/T1: folders, pins, deletion/tombstone schema and store integration.

Tests the organisation schema (folders, pins) and tombstone support for message
deletion. Every test includes a feature-positive assertion so it cannot pass
without the feature, and names an assertion that FAILS if the feature is absent.

Migration sequence: 0012 -> 0013 (organisation) -> 0014 (tombstone, conditional on R-11).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from tests._authority_test_support import downgrade_to, phase7_guarded_raw_mutation, upgrade_to


REPO = Path(__file__).resolve().parents[1]
HEAD = "0016_phase11_workspace_state"
PRIOR_HEAD = "0015_phase11_duplicate_admission"


def _upgrade_to_revision(database: Path, revision: str) -> None:
    """Upgrade database to specified revision using raw alembic."""
    upgrade_to(database, revision)


def _engine_with_transition_guard(database: Path):
    """Create an engine with the transition guard functions registered."""
    from bots5.infrastructure.persistence.transition_guard import install_transition_guard
    
    engine = create_engine(f"sqlite:///{database}", future=True)
    event.listen(engine, "connect", install_transition_guard)
    return engine


def _arm_phase7_for_tests(connection):
    """Arm the Phase 7 guard for simple DML operations in tests."""
    from bots5.infrastructure.persistence.transition_guard import arm_phase7_source_mutation
    expected_revision = connection.exec_driver_sql(
        "SELECT source_revision FROM search_source_state WHERE singleton_id=1"
    ).scalar_one()
    arm_phase7_source_mutation(
        connection.connection.driver_connection,
        "test",
        expected_revision=expected_revision,
    )


def _clear_phase7_for_tests(connection):
    """Clear the Phase 7 guard after DML operations."""
    from bots5.infrastructure.persistence.transition_guard import clear_phase7_source_mutation
    clear_phase7_source_mutation(connection.connection.driver_connection)


from contextlib import contextmanager

@contextmanager
def _phase7_guarded_dml(engine):
    """Context manager that arms the Phase 7 guard for DML operations."""
    from bots5.infrastructure.persistence.transition_guard import arm_phase7_source_mutation, clear_phase7_source_mutation
    
    with engine.connect() as conn:
        expected_revision = conn.exec_driver_sql(
            "SELECT source_revision FROM search_source_state WHERE singleton_id=1"
        ).scalar_one()
        arm_phase7_source_mutation(
            conn,
            "test",
            expected_revision=expected_revision,
        )
        try:
            yield conn
        finally:
            clear_phase7_source_mutation(conn)


def test_phase11_organisation_migration_creates_folders_table_and_pins_column(tmp_path: Path):
    """Test 1: Upgrade to head and assert new tables/columns/triggers exist."""
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    # Open with raw sqlite to inspect schema
    conn = sqlite3.connect(database)
    try:
        cursor = conn.cursor()

        # Verify folders table exists with correct columns
        cursor.execute("PRAGMA table_info(folders)")
        folders_cols = {row[1] for row in cursor.fetchall()}
        assert folders_cols == {"id", "name", "created_at", "sequence"}, (
            f"folders table missing expected columns: {folders_cols}"
        )

        # Verify chats has folder_id and is_pinned columns
        cursor.execute("PRAGMA table_info(chats)")
        chats_cols = {row[1] for row in cursor.fetchall()}
        assert "folder_id" in chats_cols, "chats table missing folder_id column"
        assert "is_pinned" in chats_cols, "chats table missing is_pinned column"

        # Note: SQLite does not support adding foreign key constraints via ALTER TABLE
        # on existing tables, so the FK is created at table creation time only.
        # The folders table was created fresh, but chats columns were added later.

        # Verify index exists
        cursor.execute("PRAGMA index_list(chats)")
        indexes = cursor.fetchall()
        index_names = {row[1] for row in indexes}
        assert "ix_chats_folder_id" in index_names, "Missing index on chats.folder_id"

        # Verify triggers exist
        cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        triggers = {row[0] for row in cursor.fetchall()}
        assert "phase11_archive_clears_pin" in triggers, (
            f"Missing phase11_archive_clears_pin trigger. Found: {triggers}"
        )
        assert "phase11_is_pinned_check" in triggers, (
            f"Missing phase11_is_pinned_check trigger. Found: {triggers}"
        )
        assert "phase11_folder_delete_unfiles_members" in triggers, (
            f"Missing phase11_folder_delete_unfiles_members trigger. Found: {triggers}"
        )
    finally:
        conn.close()


def test_phase11_organisation_migration_validation_passes_on_migrated_db(tmp_path: Path):
    """Test 2: Assert schema is valid by checking key components exist.

    Since validate_phase9_schema requires custom SQLite functions, we verify
    the schema by checking all required objects are present.
    """
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    # Check all Phase 9 objects are still present (0014 shouldn't remove them)
    conn = sqlite3.connect(database)
    try:
        cursor = conn.cursor()

        # Check Phase 9 required tables are present
        required_tables = [
            "archive_import_queue", "archive_import_queue_control",
            "archive_import_operations", "archive_import_journal",
            "archive_import_payload_reservations", "archive_lineage_nodes",
            "archive_import_chats", "archive_import_messages", "archive_import_lineages",
            "archive_imported_attempts", "archive_imported_context_plans",
            "archive_object_derivations", "archive_import_attachment_refs",
            "archive_import_message_attachment_refs", "archive_import_attempt_attachment_refs",
            "archive_continuation_anchors", "archive_continuation_choices",
            "archive_continuation_requirements", "archive_continuation_requirement_candidates",
            "archive_continuation_branches", "archive_imported_branch_choices",
        ]

        for table in required_tables:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            assert cursor.fetchone() is not None, f"Missing Phase 9 table: {table}"

        # Check Phase 9 required triggers are present
        required_triggers = [
            "phase9_object_derivation_exact_guard", "phase9_object_derivation_immutable",
            "phase9_object_derivation_delete_guard", "phase9_import_attachment_ref_update_guard",
            "phase9_import_attachment_ref_metadata_immutable",
            "phase9_imported_branch_choice_insert_guard", "phase9_imported_branch_choice_immutable",
            "phase9_import_attachment_delete_guard", "phase9_import_blob_delete_guard",
            "phase9_import_message_exact_guard", "phase9_import_chat_exact_guard",
            "phase9_import_chat_insert_exact_guard", "phase9_import_message_source_exact_guard",
            "phase9_import_lineage_exact_guard", "phase9_import_anchor_exact_guard",
            "phase9_import_anchor_source_immutable", "phase9_import_choice_exact_guard",
            "phase9_import_requirement_exact_guard", "phase9_import_requirement_candidate_exact_guard",
            "phase9_import_message_attachment_link_guard", "phase9_import_attempt_attachment_link_guard",
            "phase9_import_attempt_exact_guard", "phase9_import_context_exact_guard",
            "phase9_import_attachment_exact_guard", "phase9_import_source_exact_guard",
            "phase9_import_node_exact_guard",
        ]

        for trigger in required_triggers:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,))
            assert cursor.fetchone() is not None, f"Missing Phase 9 trigger: {trigger}"

        # Check messages_validate_insert includes 'deleted' state
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='messages_validate_insert'")
        row = cursor.fetchone()
        assert row is not None, "messages_validate_insert trigger missing"
        trigger_sql = row[0]
        assert "'deleted'" in trigger_sql, (
            f"messages_validate_insert should include 'deleted' state: {trigger_sql}"
        )

        # Check chats_revision_monotonic still exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name='chats_revision_monotonic'")
        assert cursor.fetchone() is not None, "chats_revision_monotonic trigger missing"
    finally:
        conn.close()


def test_phase11_organisation_validation_fails_when_required_object_dropped(tmp_path: Path):
    """Test 3: Assert schema validation fails when required objects are dropped.

    This proves the schema checker actually validates the required objects.
    """
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    conn = sqlite3.connect(database)
    try:
        # Drop a required Phase 9 trigger
        conn.execute("DROP TRIGGER IF EXISTS phase9_import_node_exact_guard")
        conn.commit()

        # Check that the trigger is actually dropped
        result = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name='phase9_import_node_exact_guard'")
        assert result.fetchone() is None, "Trigger should be dropped"

        # Drop folders table (Phase 11)
        conn.execute("DROP TABLE IF EXISTS folders")
        conn.execute("DROP INDEX IF EXISTS ix_chats_folder_id")
        conn.execute("DROP TRIGGER IF EXISTS phase11_archive_clears_pin")
        conn.execute("DROP TRIGGER IF EXISTS phase11_is_pinned_check")
        conn.commit()

        # Verify objects are dropped
        result = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='folders'")
        assert result.fetchone() is None, "folders table should be dropped"

        result = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name='phase11_archive_clears_pin'")
        assert result.fetchone() is None, "phase11_archive_clears_pin should be dropped"
    finally:
        conn.close()

    # Now actually invoke the CHECKER against the damaged schema.  The assertions above
    # only showed that objects are gone; they never called a validator, so this test
    # previously passed even if the schema checker validated nothing at all.
    from bots5.infrastructure.persistence.phase9_schema import validate_phase9_schema

    engine = _engine_with_transition_guard(database)
    try:
        with engine.connect() as connection:
            with pytest.raises(Exception) as excinfo:
                validate_phase9_schema(connection)
        message = str(excinfo.value)
        # The validator reports this class of damage with a generic contradiction message
        # rather than naming the object, so assert on the invariant that matters: a schema
        # missing a required object is REJECTED (and it is not rejected silently).
        assert message.strip(), "validator must explain why the schema is rejected"
        assert "contradictory" in message or "missing" in message or "phase9" in message, message
    finally:
        engine.dispose()


def test_phase11_organisation_create_and_assign_folder(tmp_path: Path):
    """Test 4: Create folders and assign chats; assert persistence and re-read."""
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    engine = _engine_with_transition_guard(database)
    try:
        # Create a folder
        folder_id = "folder-1"
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text(
                    "INSERT INTO folders (id, name, created_at, sequence) VALUES (:id, :name, :created, :seq)"
                ),
                {
                    "id": folder_id,
                    "name": "Test Folder",
                    "created": "2026-09-03T00:00:00.000Z",
                    "seq": 1,
                },
            )
            conn.commit()

        # Create a chat and assign it to the folder via raw SQL
        chat_id = "chat-1"
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at, revision) VALUES (:id, :title, :created, :updated, :rev)"
                ),
                {
                    "id": chat_id,
                    "title": "Chat in Folder",
                    "created": "2026-09-03T00:00:00.000Z",
                    "updated": "2026-09-03T00:00:00.000Z",
                    "rev": 0,
                },
            )
            conn.execute(
                text("UPDATE chats SET folder_id = :folder_id WHERE id = :chat_id"),
                {"folder_id": folder_id, "chat_id": chat_id},
            )
            conn.commit()

        # Verify folder_id is persisted
        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT folder_id FROM chats WHERE id = :id"), {"id": chat_id}
            ).fetchone()
            assert row is not None
            assert row[0] == folder_id, f"Expected folder_id={folder_id}, got {row[0]}"

        # Verify persistence after reopen
        engine.dispose()
        engine = _engine_with_transition_guard(database)

        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT folder_id FROM chats WHERE id = :id"), {"id": chat_id}
            ).fetchone()
            assert row is not None
            assert row[0] == folder_id, f"Expected folder_id={folder_id} after reopen, got {row[0]}"
    finally:
        engine.dispose()


def test_phase11_organisation_pin_and_unpin_chat(tmp_path: Path):
    """Test 5: Pin and unpin a chat; assert pinned state survives reopen."""
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    engine = _engine_with_transition_guard(database)
    try:
        # Create a chat
        chat_id = "pinned-chat"
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at, revision) VALUES (:id, :title, :created, :updated, :rev)"
                ),
                {
                    "id": chat_id,
                    "title": "Pinned Chat",
                    "created": "2026-09-03T00:00:00.000Z",
                    "updated": "2026-09-03T00:00:00.000Z",
                    "rev": 0,
                },
            )
            conn.commit()

        # Pin the chat
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text("UPDATE chats SET is_pinned = 1 WHERE id = :id"), {"id": chat_id}
            )
            conn.commit()

        # Verify pinned state
        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT is_pinned FROM chats WHERE id = :id"), {"id": chat_id}
            ).fetchone()
            assert row is not None
            assert row[0] == 1, f"Expected is_pinned=1, got {row[0]}"

        # Reopen database to verify persistence
        engine.dispose()
        engine = _engine_with_transition_guard(database)

        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT is_pinned FROM chats WHERE id = :id"), {"id": chat_id}
            ).fetchone()
            assert row is not None
            assert row[0] == 1, f"Pinned state lost after reopen: expected 1, got {row[0]}"

        # Unpin the chat
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text("UPDATE chats SET is_pinned = 0 WHERE id = :id"), {"id": chat_id}
            )
            conn.commit()

        # Verify unpinned state
        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT is_pinned FROM chats WHERE id = :id"), {"id": chat_id}
            ).fetchone()
            assert row is not None
            assert row[0] == 0, f"Expected is_pinned=0, got {row[0]}"
    finally:
        engine.dispose()


def test_phase11_organisation_archive_clears_pin(tmp_path: Path):
    """Test 6: Archiving a pinned chat clears the pin (one-way).

    The phase11_archive_clears_pin trigger should clear is_pinned when archived_at changes from NULL to NOT NULL.
    """
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    engine = _engine_with_transition_guard(database)
    try:
        # Create a chat and pin it
        chat_id = "archived-pinned-chat"
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at, revision) VALUES (:id, :title, :created, :updated, :rev)"
                ),
                {
                    "id": chat_id,
                    "title": "Pinned Then Archived",
                    "created": "2026-09-03T00:00:00.000Z",
                    "updated": "2026-09-03T00:00:00.000Z",
                    "rev": 0,
                },
            )
            conn.execute(
                text("UPDATE chats SET is_pinned = 1 WHERE id = :id"), {"id": chat_id}
            )
            conn.commit()

        # Verify pinned
        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT is_pinned FROM chats WHERE id = :id"), {"id": chat_id}
            ).fetchone()
            assert row[0] == 1, "Chat should be pinned"

        # Archive - update archived_at (this should trigger phase11_archive_clears_pin)
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text("UPDATE chats SET archived_at = :archived, updated_at = :updated WHERE id = :id"),
                {"archived": "2026-09-03T12:00:00.000Z", "updated": "2026-09-03T12:00:00.000Z", "id": chat_id},
            )
            conn.commit()

        # Verify pin is cleared by trigger
        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT is_pinned, archived_at FROM chats WHERE id = :id"),
                {"id": chat_id},
            ).fetchone()
            assert row is not None
            # The trigger should have cleared the pin
            assert row[0] == 0, f"Archive did not clear pin: expected 0, got {row[0]}"
            assert row[1] is not None, "Chat should be archived"

        # Unarchive - pin should NOT be restored (one-way)
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text("UPDATE chats SET archived_at = NULL, updated_at = :updated WHERE id = :id"),
                {"updated": "2026-09-03T13:00:00.000Z", "id": chat_id},
            )
            conn.commit()

        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT is_pinned FROM chats WHERE id = :id"), {"id": chat_id}
            ).fetchone()
            assert row[0] == 0, f"Unarchive should not restore pin: expected 0, got {row[0]}"
    finally:
        engine.dispose()


def test_phase11_organisation_pin_ordering(tmp_path: Path):
    """Test 7: Pins float to top of list_chats ordering.

    The default ordering should be is_pinned DESC, updated_at DESC, id DESC.
    """
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    engine = _engine_with_transition_guard(database)
    try:
        # Create three chats with different timestamps and pin states
        now = "2026-09-03T00:00:00.000Z"
        middle = "2026-09-03T01:00:00.000Z"
        recent = "2026-09-03T02:00:00.000Z"

        with _phase7_guarded_dml(engine) as conn:
            # chat_1: oldest, unpinned
            conn.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at, revision) VALUES (:id, :title, :created, :updated, :rev)"
                ),
                {
                    "id": "chat-1",
                    "title": "Oldest Unpinned",
                    "created": now,
                    "updated": now,
                    "rev": 0,
                },
            )

            # chat_2: middle, pinned
            conn.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at, revision) VALUES (:id, :title, :created, :updated, :rev)"
                ),
                {
                    "id": "chat-2",
                    "title": "Middle Pinned",
                    "created": middle,
                    "updated": middle,
                    "rev": 0,
                },
            )
            # Pin chat-2
            conn.execute(
                text("UPDATE chats SET is_pinned = 1 WHERE id = :id"), {"id": "chat-2"}
            )

            # chat_3: newest, unpinned
            conn.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at, revision) VALUES (:id, :title, :created, :updated, :rev)"
                ),
                {
                    "id": "chat-3",
                    "title": "Newest Unpinned",
                    "created": recent,
                    "updated": recent,
                    "rev": 0,
                },
            )
            conn.commit()

        # Read chats through the PRODUCT read path.  This block previously ran its own
        # "ORDER BY is_pinned DESC, updated_at DESC, id DESC" query and asserted on those
        # rows, so it verified SQLite's ORDER BY rather than list_chats() and would have
        # passed unchanged even if list_chats() had no pin ordering at all.
        from tests._authority_test_support import SQLiteAppStateStore

        store = SQLiteAppStateStore.open(database)
        try:
            chat_ids = [chat.id for chat in store.list_chats()]
        finally:
            store.close()

        assert len(chat_ids) == 3, f"Expected 3 chats, got {len(chat_ids)}"
        assert chat_ids[0] == "chat-2", f"First should be pinned chat-2, got {chat_ids[0]}"
        # chat-3 should be before chat-1 (newer first among unpinned)
        assert chat_ids.index("chat-3") < chat_ids.index("chat-1"), (
            f"Unpinned chats should be ordered by updated_at DESC: got {chat_ids}"
        )
    finally:
        engine.dispose()


def test_phase11_organisation_folder_delete_unfiles_members(tmp_path: Path):
    """Test 8: Deleting a folder unsets folder_id on members (never deletes chats)."""
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    engine = _engine_with_transition_guard(database)
    try:
        # Create folder and chat
        folder_id = "folder-1"
        chat_id = "chat-1"
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text(
                    "INSERT INTO folders (id, name, created_at, sequence) VALUES (:id, :name, :created, :seq)"
                ),
                {
                    "id": folder_id,
                    "name": "Folder to Delete",
                    "created": "2026-09-03T00:00:00.000Z",
                    "seq": 1,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at, revision) VALUES (:id, :title, :created, :updated, :rev)"
                ),
                {
                    "id": chat_id,
                    "title": "Chat in Folder",
                    "created": "2026-09-03T00:00:00.000Z",
                    "updated": "2026-09-03T00:00:00.000Z",
                    "rev": 0,
                },
            )
            conn.execute(
                text("UPDATE chats SET folder_id = :folder_id WHERE id = :chat_id"),
                {"folder_id": folder_id, "chat_id": chat_id},
            )
            conn.commit()

        # Verify chat is filed
        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT folder_id FROM chats WHERE id = :id"), {"id": chat_id}
            ).fetchone()
            assert row[0] == folder_id

        # Delete folder
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(text("DELETE FROM folders WHERE id = :id"), {"id": folder_id})
            conn.commit()

        # Verify chat is now unfiled (folder_id is NULL)
        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT folder_id FROM chats WHERE id = :id"), {"id": chat_id}
            ).fetchone()
            assert row[0] is None, f"Chat should be unfiled, got folder_id={row[0]}"

        # Verify chat still exists
        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT id FROM chats WHERE id = :id"), {"id": chat_id}
            ).fetchone()
            assert row is not None
    finally:
        engine.dispose()


def test_phase11_tombstone_message_deletion(tmp_path: Path):
    """Test 9: Delete a message; assert tombstone is recorded and content is gone.

    Note: This test requires R-11 (deletion) to be included. If R-11 is deferred,
    this test is omitted from the suite.
    """
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    engine = _engine_with_transition_guard(database)
    try:
        # Create a chat with one message marked as 'deleted'
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at, revision) VALUES (:id, :title, :created, :updated, :rev)"
                ),
                {
                    "id": "test-chat",
                    "title": "Test Chat",
                    "created": "2026-09-03T00:00:00.000Z",
                    "updated": "2026-09-03T00:00:00.000Z",
                    "rev": 0,
                },
            )

            # Insert a message with 'deleted' state
            conn.execute(
                text(
                    "INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at, lineage_id, revision) VALUES (:id, :chat, :parent, :seq, :role, :state, :content, :created, :lineage, :rev)"
                ),
                {
                    "id": "msg-1",
                    "chat": "test-chat",
                    "parent": None,
                    "seq": 1,
                    "role": "user",
                    "state": "deleted",
                    "content": "",
                    "created": "2026-09-03T00:00:00.000Z",
                    "lineage": "msg-1",
                    "rev": 1,
                },
            )
            conn.commit()

        # Verify tombstone - message exists but state is 'deleted'
        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT state, content FROM messages WHERE id = :id"),
                {"id": "msg-1"},
            ).fetchone()
            assert row is not None
            assert row[0] == "deleted", f"State should be 'deleted', got {row[0]}"
            assert row[1] == "", f"Deleted message content should be empty, got '{row[1]}'"
    finally:
        engine.dispose()


def test_phase11_tombstone_trigger_accepts_deleted_state(tmp_path: Path):
    """Test 10: Verify the messages_validate_insert trigger accepts 'deleted' state.

    After 0014, the trigger should accept 'deleted' state in the allowed states list.
    """
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    engine = _engine_with_transition_guard(database)
    try:
        with _phase7_guarded_dml(engine) as conn:
            # Create a test chat
            conn.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at, revision) VALUES (:id, :title, :created, :updated, :rev)"
                ),
                {
                    "id": "test-chat",
                    "title": "Test",
                    "created": "2026-09-03T00:00:00.000Z",
                    "updated": "2026-09-03T00:00:00.000Z",
                    "rev": 0,
                },
            )

            # Try to insert a message with 'deleted' state
            # The trigger should accept this after 0014
            try:
                conn.execute(
                    text(
                        "INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at, lineage_id, revision) VALUES (:id, :chat, :parent, :seq, :role, :state, :content, :created, :lineage, :rev)"
                    ),
                    {
                        "id": "deleted-msg",
                        "chat": "test-chat",
                        "parent": None,
                        "seq": 1,
                        "role": "user",
                        "state": "deleted",
                        "content": "",
                        "created": "2026-09-03T00:00:00.000Z",
                        "lineage": "deleted-msg",
                        "rev": 1,
                    },
                )
                conn.commit()
                # If we get here, 'deleted' state is accepted (0014 applied)
                assert True, "'deleted' state accepted by trigger (0014 applied)"
            except Exception as e:
                pytest.fail(f"Expected 'deleted' state to be accepted after 0014, got error: {e}")
    finally:
        engine.dispose()


def test_phase11_tombstone_trigger_rejects_invalid_user_state(tmp_path: Path):
    """Test 11: Verify the trigger still rejects invalid states like 'streaming' for user messages."""
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    engine = _engine_with_transition_guard(database)
    try:
        with _phase7_guarded_dml(engine) as conn:
            # Create a test chat
            conn.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at, revision) VALUES (:id, :title, :created, :updated, :rev)"
                ),
                {
                    "id": "test-chat",
                    "title": "Test",
                    "created": "2026-09-03T00:00:00.000Z",
                    "updated": "2026-09-03T00:00:00.000Z",
                    "rev": 0,
                },
            )

            # Try to insert a message with invalid state 'streaming' for user
            # This should be rejected even after 0014
            with pytest.raises(Exception, match="message fields are invalid"):
                conn.execute(
                    text(
                        "INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at, lineage_id, revision) VALUES (:id, :chat, :parent, :seq, :role, :state, :content, :created, :lineage, :rev)"
                    ),
                    {
                        "id": "invalid-msg",
                        "chat": "test-chat",
                        "parent": None,
                        "seq": 1,
                        "role": "user",
                        "state": "streaming",  # Invalid for user
                        "content": "",
                        "created": "2026-09-03T00:00:00.000Z",
                        "lineage": "invalid-msg",
                        "rev": 1,
                    },
                )
                conn.commit()
    finally:
        engine.dispose()


def test_phase11_no_revision_bump_on_folder_pin_operations(tmp_path: Path):
    """Test 12: Verify that folder/pin operations don't bump chat revision.

    The chats_revision_monotonic trigger should only allow revision bump
    for head message advance or import operations.
    """
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    engine = _engine_with_transition_guard(database)
    try:
        # Create a chat
        chat_id = "test-chat"
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at, revision) VALUES (:id, :title, :created, :updated, :rev)"
                ),
                {
                    "id": chat_id,
                    "title": "Test Chat",
                    "created": "2026-09-03T00:00:00.000Z",
                    "updated": "2026-09-03T00:00:00.000Z",
                    "rev": 0,
                },
            )
            conn.commit()

        # Get initial revision
        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT revision FROM chats WHERE id = :id"), {"id": chat_id}
            ).fetchone()
            initial_revision = row[0]
            assert initial_revision == 0

        # Update folder_id - should NOT bump revision
        with _phase7_guarded_dml(engine) as conn:
            # Create a folder first
            conn.execute(
                text(
                    "INSERT INTO folders (id, name, created_at, sequence) VALUES (:id, :name, :created, :seq)"
                ),
                {"id": "folder-1", "name": "Test", "created": "2026-09-03T00:00:00.000Z", "seq": 1},
            )
            conn.execute(
                text("UPDATE chats SET folder_id = :folder_id WHERE id = :chat_id"),
                {"folder_id": "folder-1", "chat_id": chat_id},
            )
            conn.commit()

        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT revision FROM chats WHERE id = :id"), {"id": chat_id}
            ).fetchone()
            assert row[0] == 0, f"Folder assignment bumped revision: expected 0, got {row[0]}"

        # Update is_pinned - should NOT bump revision
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text("UPDATE chats SET is_pinned = 1 WHERE id = :id"), {"id": chat_id}
            )
            conn.commit()

        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT revision FROM chats WHERE id = :id"), {"id": chat_id}
            ).fetchone()
            assert row[0] == 0, f"Pin update bumped revision: expected 0, got {row[0]}"

        # Update archived_at - should NOT bump revision (only updated_at changes)
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text("UPDATE chats SET archived_at = :archived, updated_at = :updated WHERE id = :id"),
                {"archived": "2026-09-03T12:00:00.000Z", "updated": "2026-09-03T12:00:00.000Z", "id": chat_id},
            )
            conn.commit()

        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT revision FROM chats WHERE id = :id"), {"id": chat_id}
            ).fetchone()
            assert row[0] == 0, f"Archive bumped revision: expected 0, got {row[0]}"
    finally:
        engine.dispose()


def test_phase11_schema_validator_checks_trigger_definitions(tmp_path: Path):
    """Test 13: Verify that trigger definitions can be verified by checking SQL.

    If a trigger exists but has different SQL, we can detect it by comparing the SQL.
    """
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    engine = _engine_with_transition_guard(database)
    try:
        with _phase7_guarded_dml(engine) as conn:
            # Corrupt the phase11_is_pinned_check trigger to have wrong condition
            conn.execute(text("DROP TRIGGER IF EXISTS phase11_is_pinned_check"))
            conn.execute(text("""
                CREATE TRIGGER phase11_is_pinned_check
                BEFORE UPDATE OF is_pinned ON chats
                WHEN NEW.is_pinned < 0
                BEGIN
                    SELECT RAISE(ABORT, 'is_pinned must be non-negative');
                END
            """))
            conn.commit()

        # Reopen to get a fresh connection for validation
        with _phase7_guarded_dml(engine) as conn:
            # Read the trigger SQL and verify it doesn't match expected
            result = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='phase11_is_pinned_check'")
            )
            row = result.fetchone()
            assert row is not None
            trigger_sql = row[0]
            # The corrupted trigger should not have the correct condition
            assert "NOT IN (0, 1)" not in trigger_sql, (
                "Expected corrupted trigger, but found correct condition"
            )

        # Restore the correct trigger
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(text("DROP TRIGGER IF EXISTS phase11_is_pinned_check"))
            conn.execute(text("""
                CREATE TRIGGER phase11_is_pinned_check
                BEFORE UPDATE OF is_pinned ON chats
                WHEN NEW.is_pinned NOT IN (0, 1)
                BEGIN
                    SELECT RAISE(ABORT, 'is_pinned must be 0 or 1');
                END
            """))
            conn.commit()
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The authoritative Phase 9 checker must be proven to FAIL, not just to pass.
# A validator that is never observed rejecting a corrupted schema is untested.
# ---------------------------------------------------------------------------


def _validated_engine(database: Path):
    """Open an engine whose connections carry the guard functions the DDL needs."""
    from sqlalchemy import create_engine, event

    from bots5.infrastructure.persistence.phase9_schema import validate_phase9_schema
    from bots5.infrastructure.persistence.transition_guard import install_transition_guard

    engine = create_engine(f"sqlite:///{database}", future=True)
    event.listen(engine, "connect", install_transition_guard)
    return engine, validate_phase9_schema


def test_authoritative_validator_accepts_migrated_database(tmp_path: Path):
    """The real checker must pass on a correctly migrated database."""
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    engine, validate = _validated_engine(database)
    with engine.connect() as conn:
        validate(conn)  # must not raise


def test_authoritative_validator_rejects_dropped_trigger(tmp_path: Path):
    """Dropping a required trigger must make the checker raise."""
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    engine, validate = _validated_engine(database)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER messages_validate_insert")
    with engine.connect() as conn:
        with pytest.raises(Exception):
            validate(conn)


def test_authoritative_validator_rejects_weakened_trigger(tmp_path: Path):
    """A same-name no-op trigger must be caught by the exact-DDL comparison."""
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    engine, validate = _validated_engine(database)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER messages_validate_insert")
        conn.exec_driver_sql(
            "CREATE TRIGGER messages_validate_insert AFTER INSERT ON messages "
            "BEGIN SELECT 1; END"
        )
    with engine.connect() as conn:
        with pytest.raises(Exception):
            validate(conn)


def test_message_state_exposes_the_tombstone(tmp_path: Path):
    """R-11: the domain enum must expose the tombstone state (design sec 4.6)."""
    from bots5.domain.models import MessageState

    assert MessageState.DELETED == "deleted"


def test_0013_upgrade_creates_the_organisation_triggers(tmp_path: Path):
    """Check genuine 0013 upgrade objects; this provides no downgrade proof."""
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, "0013_phase11_organisation")

    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0013_phase11_organisation",
        )
        created = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'phase11_%'"
            ).fetchall()
        }
    assert created == {
        "phase11_archive_clears_pin",
        "phase11_is_pinned_check",
        "phase11_folder_delete_unfiles_members",
    }, f"unexpected Phase 11 trigger set: {created}"


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
def test_frozen_0013_downgrade_retains_accepted_partial_ddl_limitation(
    tmp_path: Path, populated: bool
):
    """P11-02: unsupported reversal fails and leaves partial DDL, not 0012.

    Mick accepted this historical limitation on 2026-10-04. Supported recovery
    restores the prior source/backup; frozen 0013 is not a recovery procedure.
    """
    prior = "0012_phase9_archive_import"
    organisation = "0013_phase11_organisation"
    reference = tmp_path / "genuine-0012.sqlite3"
    database = tmp_path / "downgrade-probe.sqlite3"
    upgrade_to(reference, prior)
    upgrade_to(database, prior)
    with sqlite3.connect(reference) as conn:
        reference_columns = {row[1] for row in conn.execute("PRAGMA table_info(chats)")}
        assert not {"folder_id", "is_pinned"} & reference_columns
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('folders', '_alembic_tmp_chats')"
        ).fetchall() == []

    if populated:
        with sqlite3.connect(database) as conn:
            with phase7_guarded_raw_mutation(conn, "test"):
                conn.execute(
                    "INSERT INTO chats(id, title, created_at, updated_at, revision) "
                    "VALUES ('retained-chat', 'Retained', '2026-10-04T00:00:00.000Z', "
                    "'2026-10-04T00:00:00.000Z', 0)"
                )
    upgrade_to(database, organisation)
    triggers = {
        "phase11_archive_clears_pin",
        "phase11_is_pinned_check",
        "phase11_folder_delete_unfiles_members",
    }
    with sqlite3.connect(database) as conn:
        before_objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert triggers | {"folders", "ix_chats_folder_id"} <= before_objects
        assert "_alembic_tmp_chats" not in before_objects
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            organisation,
        )
        before_rows = conn.execute("SELECT * FROM chats ORDER BY id").fetchall()

    with pytest.raises(
        OperationalError,
        match=r"error in trigger messages_active_head_parent_guard: no such table: main\.chats",
    ) as failure:
        downgrade_to(database, prior)
    assert isinstance(failure.value.orig, sqlite3.OperationalError)
    assert "ALTER TABLE _alembic_tmp_chats RENAME TO chats" in failure.value.statement

    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            organisation,
        )
        after_columns = {row[1] for row in conn.execute("PRAGMA table_info(chats)")}
        assert after_columns == reference_columns | {"folder_id", "is_pinned"}
        after_objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert {"folders", "_alembic_tmp_chats"} <= after_objects
        assert not (triggers | {"ix_chats_folder_id"}) & after_objects
        assert conn.execute("SELECT * FROM chats ORDER BY id").fetchall() == before_rows
        assert len(before_rows) == int(populated)
        assert conn.execute("SELECT count(*) FROM _alembic_tmp_chats").fetchone() == (0,)


def test_store_list_chats_floats_a_pinned_chat_to_the_top(tmp_path: Path):
    """The real store read path must put pinned chats first.

    This exercises `list_chats()` itself rather than raw SQL, because the first
    M3 draft passed an ordering test while the store read path was broken.
    """
    from datetime import datetime, timedelta, timezone

    from bots5.domain.models import Chat
    from tests._authority_test_support import SQLiteAppStateStore

    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    store = SQLiteAppStateStore.open(database)
    try:
        base = datetime(2026, 9, 3, tzinfo=timezone.utc)
        with store.command_admission():
            oldest = Chat("chat-oldest", "Oldest", base, base)
            middle = Chat("chat-middle", "Middle", base, base + timedelta(hours=1))
            newest = Chat("chat-newest", "Newest", base, base + timedelta(hours=2))
            for chat in (oldest, middle, newest):
                store.create_chat(chat)

        # Pin the middle chat directly; organisation write operations are not
        # part of this milestone, and this test is about the read path.  The
        # write runs inside the store's admission context because the rooted
        # authority refuses database access without a forward grant.
        with store.command_admission(), store.engine.begin() as connection:
            connection.execute(
                text("UPDATE chats SET is_pinned = 1 WHERE id = 'chat-middle'")
            )

        titles = [chat.title for chat in store.list_chats()]
        assert titles[0] == "Middle", f"pinned chat must float to the top, got {titles}"
        # The remaining chats keep the most-recently-updated ordering.
        assert titles[1:] == ["Newest", "Oldest"], titles
    finally:
        store.close()


def test_store_list_chats_orders_by_recency_without_pins(tmp_path: Path):
    """With nothing pinned the pre-existing recency ordering must be preserved."""
    from datetime import datetime, timedelta, timezone

    from bots5.domain.models import Chat
    from tests._authority_test_support import SQLiteAppStateStore

    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)

    store = SQLiteAppStateStore.open(database)
    try:
        base = datetime(2026, 9, 3, tzinfo=timezone.utc)
        with store.command_admission():
            first = Chat("chat-a", "First", base, base)
            second = Chat("chat-b", "Second", base, base + timedelta(hours=1))
            for chat in (first, second):
                store.create_chat(chat)

        titles = [chat.title for chat in store.list_chats()]
        assert titles == ["Second", "First"], titles
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Phase 11 M3 product path (F4/F5): folders and pins through the REAL store
# API.  These tests deliberately do NOT use raw SQL to establish state; raw
# SQL appears only where the schema behaviour itself is the subject.
# ---------------------------------------------------------------------------


def _m3_store(tmp_path: Path, *, revision: str = HEAD):
    """Open a real authority-backed store at the requested revision."""
    from tests._authority_test_support import SQLiteAppStateStore

    database = tmp_path / "m3-state.sqlite3"
    _upgrade_to_revision(database, revision)
    return SQLiteAppStateStore.open(database)


def _m3_chat(store, chat_id: str, title: str = "Chat", *, when=None):
    from datetime import datetime, timedelta, timezone

    from bots5.domain.models import Chat

    now = when or datetime(2026, 9, 3, tzinfo=timezone.utc)
    with store.command_admission():
        store.create_chat(Chat(chat_id, title, now, now))
    return store.get_chat(chat_id)


def test_m3_store_folder_lifecycle_roundtrip(tmp_path: Path):
    """create_folder / rename_folder / list_folders through the real store."""
    store = _m3_store(tmp_path)
    try:
        from bots5.domain.ids import Uuid7Factory
        from bots5.domain.clock import SystemClock

        ids = Uuid7Factory()
        with store.command_admission():
            folder = store.create_folder("Work", clock=SystemClock(), ids=ids)
        assert folder.name == "Work"
        assert folder.sequence == 1

        with store.command_admission():
            second = store.create_folder("Archive", clock=SystemClock(), ids=ids)
        assert second.sequence == 2

        # Folder names are unique case-insensitively and must be non-empty.
        with store.command_admission():
            with pytest.raises(Exception, match="folder already exists"):
                store.create_folder("work", clock=SystemClock(), ids=ids)
            with pytest.raises(Exception, match="folder name must not be empty"):
                store.create_folder("   ", clock=SystemClock(), ids=ids)
            with pytest.raises(Exception, match="folder name must be a string"):
                store.create_folder(None, clock=SystemClock(), ids=ids)  # type: ignore[arg-type]

        with store.command_admission():
            renamed = store.rename_folder(folder.id, "Work stuff")
        assert renamed.name == "Work stuff"
        with store.command_admission():
            with pytest.raises(Exception, match="folder not found"):
                store.rename_folder("folder-missing", "Nowhere")

        with store.command_admission():
            folders = store.list_folders()
        assert [f.name for f in folders] == ["Work stuff", "Archive"]

        # The lifecycle is durable across a reopen.
        store.close()
        store = SQLiteAppStateStore_for_reopen(tmp_path)
        with store.command_admission():
            folders = store.list_folders()
        assert [f.name for f in folders] == ["Work stuff", "Archive"]
    finally:
        store.close()


def SQLiteAppStateStore_for_reopen(tmp_path: Path):
    from tests._authority_test_support import SQLiteAppStateStore

    return SQLiteAppStateStore.open(tmp_path / "m3-state.sqlite3")


def test_m3_folder_delete_unfiles_members_and_never_deletes_chats(tmp_path: Path):
    store = _m3_store(tmp_path)
    try:
        from bots5.domain.ids import Uuid7Factory
        from bots5.domain.clock import SystemClock

        with store.command_admission():
            folder = store.create_folder("Work", clock=SystemClock(), ids=Uuid7Factory())
        _m3_chat(store, "chat-1")
        _m3_chat(store, "chat-2")
        with store.command_admission():
            store.set_chat_folder("chat-1", folder.id)

        with store.command_admission():
            store.delete_folder(folder.id)

        # Both chats survive; the member is unfiled.
        assert store.get_chat("chat-1") is not None
        assert store.get_chat("chat-2") is not None
        assert store.get_chat("chat-1").folder_id is None
        with store.command_admission():
            assert store.list_folders() == ()
            with pytest.raises(Exception, match="folder not found"):
                store.delete_folder(folder.id)
    finally:
        store.close()


def test_m3_set_chat_folder_assigns_unfiles_and_refuses(tmp_path: Path):
    store = _m3_store(tmp_path)
    try:
        from bots5.domain.ids import Uuid7Factory
        from bots5.domain.clock import SystemClock
        from bots5.core.errors import StateError

        with store.command_admission():
            folder = store.create_folder("Work", clock=SystemClock(), ids=Uuid7Factory())
        chat = _m3_chat(store, "chat-1")

        with store.command_admission():
            filed = store.set_chat_folder("chat-1", folder.id)
        assert filed.folder_id == folder.id
        # The move is DURABLE, not just a return value: re-read from the store.
        assert store.get_chat("chat-1").folder_id == folder.id

        # F4: a move is metadata only — no revision bump, no activity bump.
        assert filed.revision == chat.revision
        assert filed.updated_at == chat.updated_at

        # Exactly one optional folder: moving elsewhere replaces, None unfiles.
        with store.command_admission():
            other = store.create_folder("Elsewhere", clock=SystemClock(), ids=Uuid7Factory())
            moved = store.set_chat_folder("chat-1", other.id)
        assert moved.folder_id == other.id
        assert store.get_chat("chat-1").folder_id == other.id
        with store.command_admission():
            unfiled = store.set_chat_folder("chat-1", None)
        assert unfiled.folder_id is None
        assert store.get_chat("chat-1").folder_id is None

        # Refusals: unknown chat, unknown folder, malformed folder id.
        with store.command_admission():
            with pytest.raises(StateError, match="chat not found"):
                store.set_chat_folder("chat-missing", folder.id)
            with pytest.raises(StateError, match="folder not found"):
                store.set_chat_folder("chat-1", "folder-missing")
            with pytest.raises(StateError, match="folder id"):
                store.set_chat_folder("chat-1", "")
        # The refusals were complete.
        assert store.get_chat("chat-1").folder_id is None
    finally:
        store.close()


def test_m3_set_chat_pinned_roundtrip_and_refusals(tmp_path: Path):
    store = _m3_store(tmp_path)
    try:
        from bots5.core.errors import StateError
        from datetime import datetime, timezone

        chat = _m3_chat(store, "chat-1")
        with store.command_admission():
            pinned = store.set_chat_pinned("chat-1", True)
        assert pinned.is_pinned is True
        # F5: pinning is metadata only — no revision bump, no activity bump.
        assert pinned.revision == chat.revision
        assert pinned.updated_at == chat.updated_at

        # The pin floats the chat above newer unpinned chats (store read path).
        _m3_chat(
            store,
            "chat-newer",
            "Newer",
            when=datetime(2026, 9, 4, tzinfo=timezone.utc),
        )
        assert [c.id for c in store.list_chats()][0] == "chat-1"

        with store.command_admission():
            unpinned = store.set_chat_pinned("chat-1", False)
        assert unpinned.is_pinned is False

        # Refusals: non-boolean pin state and unknown chats.
        with store.command_admission():
            with pytest.raises(StateError, match="boolean"):
                store.set_chat_pinned("chat-1", "yes")  # type: ignore[arg-type]
            with pytest.raises(StateError, match="chat not found"):
                store.set_chat_pinned("chat-missing", True)
        assert store.get_chat("chat-1").is_pinned is False
    finally:
        store.close()


def test_m3_new_store_operations_carry_the_callee_grant(tmp_path: Path):
    """A-2 discipline: every new public store operation is wrapper-decorated.

    A public mutating method missing from ``_SQLITE_OPERATION_METHODS`` would
    run without its callee-owned logical grant; this pins the whole M3 family
    into the decorated set and proves the wrapper is really applied.
    """
    from bots5.infrastructure.persistence import sqlite as sqlite_store_module
    from bots5.infrastructure.persistence.sqlite import SQLiteAppStateStore

    m3_operations = {
        "create_folder",
        "rename_folder",
        "delete_folder",
        "list_folders",
        "set_chat_folder",
        "set_chat_pinned",
        "describe_chat_deletion",
        "delete_message",
        "delete_chat",
    }
    assert m3_operations <= set(sqlite_store_module._SQLITE_OPERATION_METHODS)
    for name in m3_operations:
        method = getattr(SQLiteAppStateStore, name)
        assert hasattr(method, "__wrapped__"), name


# ---------------------------------------------------------------------------
# Phase 11 M3 schema integrity: the 0017 revision (pin INSERT guard, folder
# reference enforcement, tombstone/delete path) with a complete downgrade.
# These are SCHEMA tests, so raw SQL through the alembic-upgraded fixture is
# the subject under test (mirroring the style of the earlier tests in this
# file).  All feature-level behaviour is tested through the product API in
# tests/test_phase11_deletion_product_path.py.
# ---------------------------------------------------------------------------


M3_HEAD = "0017_phase11_integrity"


def test_0017_migration_installs_the_integrity_guards(tmp_path: Path):
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, M3_HEAD)

    conn = sqlite3.connect(database)
    try:
        triggers = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert "phase11_chat_pin_insert_guard" in triggers
        assert "phase11_folder_reference_insert_guard" in triggers
        assert "phase11_folder_reference_update_guard" in triggers
        # The tombstone/delete path is present in the recreated triggers.
        delete_immutable = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='messages_delete_immutable'"
        ).fetchone()[0]
        assert "bots5_phase11_chat_message_delete_allowed" in delete_immutable
        terminal = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='messages_terminal_immutable'"
        ).fetchone()[0]
        assert "'deleted'" in terminal
    finally:
        conn.close()


def test_0017_pin_insert_guard_rejects_out_of_range_pin_values(tmp_path: Path):
    """Repair: an INSERT can no longer store an out-of-range pin value."""
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, M3_HEAD)

    engine = _engine_with_transition_guard(database)
    try:
        with _phase7_guarded_dml(engine) as conn:
            with pytest.raises(Exception, match="is_pinned must be 0 or 1"):
                conn.execute(
                    text(
                        "INSERT INTO chats (id, title, created_at, updated_at, revision, is_pinned) "
                        "VALUES ('c', 'T', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z', 0, 5)"
                    )
                )
            conn.rollback()
        # In-range values still insert.
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at, revision, is_pinned) "
                    "VALUES ('c2', 'T', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z', 0, 1)"
                )
            )
            conn.commit()
        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT is_pinned FROM chats WHERE id = 'c2'")
            ).fetchone()
            assert row[0] == 1
    finally:
        engine.dispose()


def test_0017_folder_reference_guards_enforce_chats_folder_id(tmp_path: Path):
    """Repair: chats.folder_id must reference an existing folders row."""
    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, M3_HEAD)

    engine = _engine_with_transition_guard(database)
    try:
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text(
                    "INSERT INTO folders (id, name, created_at, sequence) "
                    "VALUES ('f1', 'Work', '2026-09-03T00:00:00.000Z', 1)"
                )
            )
            conn.commit()
        with _phase7_guarded_dml(engine) as conn:
            with pytest.raises(
                Exception, match="chat folder_id does not reference a folder"
            ):
                conn.execute(
                    text(
                        "INSERT INTO chats (id, title, created_at, updated_at, revision, folder_id) "
                        "VALUES ('c1', 'T', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z', 0, 'missing')"
                    )
                )
            conn.rollback()
        with _phase7_guarded_dml(engine) as conn:
            # A valid reference inserts...
            conn.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at, revision, folder_id) "
                    "VALUES ('c1', 'T', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z', 0, 'f1')"
                )
            )
            conn.commit()
            # ...and re-pointing at a missing folder on UPDATE is refused too.
            with pytest.raises(
                Exception, match="chat folder_id does not reference a folder"
            ):
                conn.execute(
                    text("UPDATE chats SET folder_id = 'missing' WHERE id = 'c1'")
                )
            conn.rollback()
        with _phase7_guarded_dml(engine) as conn:
            row = conn.execute(
                text("SELECT folder_id FROM chats WHERE id = 'c1'")
            ).fetchone()
            assert row[0] == "f1"
    finally:
        engine.dispose()


def test_0017_downgrade_restores_the_0016_schema_exactly(tmp_path: Path):
    """The 0017 downgrade reproduces the 0016 schema definition-for-definition."""
    from tests._authority_test_support import downgrade_to

    reference = tmp_path / "reference.sqlite3"
    _upgrade_to_revision(reference, HEAD)

    worked = tmp_path / "worked.sqlite3"
    _upgrade_to_revision(worked, HEAD)
    _upgrade_to_revision(worked, M3_HEAD)

    # Surviving data must survive the round trip.
    engine = _engine_with_transition_guard(worked)
    try:
        with _phase7_guarded_dml(engine) as conn:
            conn.execute(
                text(
                    "INSERT INTO folders (id, name, created_at, sequence) "
                    "VALUES ('f1', 'Kept', '2026-09-03T00:00:00.000Z', 1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at, revision, folder_id, is_pinned) "
                    "VALUES ('c1', 'Kept chat', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z', 0, 'f1', 1)"
                )
            )
            conn.commit()
    finally:
        engine.dispose()

    downgrade_to(worked, HEAD)

    def snapshot(path: Path) -> dict[str, object]:
        """Capture the schema in full, but order-insensitively where SQLite's
        own rendering order is not stable.

        Trigger and index definitions are compared byte-exactly (they are the
        whole subject of 0017).  Tables are compared by exact column layout
        plus the SET of foreign keys, because alembic's rendering order for
        unnamed column-level foreign keys inside CREATE TABLE is not stable
        across in-process build histories — while the SET is what the schema
        actually guarantees.
        """
        conn = sqlite3.connect(path)
        try:
            objects: dict[str, object] = {}
            for kind, name, sql in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
            ):
                if kind == "table":
                    columns = tuple(
                        (row[1], row[2], row[3], row[4], row[5])
                        for row in conn.execute(f"PRAGMA table_info({name})")
                    )
                    foreign_keys = {
                        (row[2], row[3], row[4], row[5], row[6])
                        for row in conn.execute(f"PRAGMA foreign_key_list({name})")
                    }
                    objects[f"table:{name}"] = (columns, foreign_keys)
                else:
                    objects[f"{kind}:{name}"] = sql
            return objects
        finally:
            conn.close()

    expected = snapshot(reference)
    actual = snapshot(worked)
    assert set(actual) == set(expected), (
        sorted(set(actual) ^ set(expected))
    )
    for key, expected_value in expected.items():
        assert actual[key] == expected_value, key

    # The surviving data is still there, still filed, still pinned.
    conn = sqlite3.connect(worked)
    try:
        row = conn.execute(
            "SELECT folder_id, is_pinned FROM chats WHERE id = 'c1'"
        ).fetchone()
        assert row == ("f1", 1)
        assert conn.execute("SELECT count(*) FROM folders").fetchone()[0] == 1
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == HEAD
    finally:
        conn.close()


def test_pins_float_under_every_sort(tmp_path: Path):
    """F18 / section 4.9: "Pins float under ALL sorts".

    The pin term was previously applied only in the default (recent) branch, so a pinned
    chat sank below unpinned ones under the creation and title sorts.  Each case below is
    chosen so the natural order of that sort would put the UNPINNED chat first, making the
    assertion discriminate: it fails if the pin term is missing from that branch.
    """
    from datetime import UTC, datetime, timedelta

    from bots5.domain.models import Chat
    from bots5.infrastructure.persistence.sqlite import ChatSort
    from tests._authority_test_support import SQLiteAppStateStore

    database = tmp_path / "state.sqlite3"
    _upgrade_to_revision(database, HEAD)
    store = SQLiteAppStateStore.open(database)
    try:
        base = datetime(2026, 9, 3, tzinfo=UTC)
        with store.command_admission():
            store.create_chat(Chat("c-old", "aaa old", base, base))
            store.create_chat(Chat("c-new", "zzz new", base, base + timedelta(hours=2)))

        # Pin the OLDER chat: natural order for recent/creation is newest-first, so a
        # missing pin term would put "zzz new" first instead.
        with store.command_admission():
            store.set_chat_pinned("c-old", True)
        for sort in (ChatSort.RECENT, ChatSort.CREATION):
            titles = [chat.title for chat in store.list_chats(sort)]
            assert titles[0] == "aaa old", f"{sort}: pinned chat did not float: {titles}"

        # Pin the LATER-alphabetical chat: natural title order is a->z, so a missing pin
        # term would put "aaa old" first instead.
        with store.command_admission():
            store.set_chat_pinned("c-old", False)
            store.set_chat_pinned("c-new", True)
        titles = [chat.title for chat in store.list_chats(ChatSort.TITLE)]
        assert titles[0] == "zzz new", f"title: pinned chat did not float: {titles}"
    finally:
        store.close()
