"""Phase 11 M4b/M6: Workspace state and settings persistence.

Tests the Phase 11 workspace/settings persistence tables:
- maximized state, scroll position for workspace_windows
- chat_drafts for chat draft text
- dock_layout for QMainWindow.saveState() opaque blob
- keybinding_overrides for keybinding conflicts and overrides
- font_scale_settings for font/scale configuration

Every test includes a feature-positive assertion so it cannot pass
without the feature, and names an assertion that FAILS if the feature is absent.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timezone
from pathlib import Path

import pytest

# Set Qt platform before any Qt imports
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = Path(__file__).resolve().parents[1]
HEAD = "0020_provider_managed_context"
PRIOR_HEAD = "0015_phase11_duplicate_admission"

# --- Phase 11 M0a/M4b/M6 desktop wiring test support ------------------------
# Qt imports stay below the QT_QPA_PLATFORM default set above.
import asyncio  # noqa: E402

from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from qasync import QEventLoop  # noqa: E402

from bots5.core.application import BotsApplication  # noqa: E402
from bots5.core.events import EventBus  # noqa: E402
from bots5.desktop.theme import build_theme_stylesheet  # noqa: E402
from bots5.desktop.window import (  # noqa: E402
    MainWindow,
    clamp_geometry_to_available_screens,
)
from bots5.domain.clock import SystemClock  # noqa: E402
from bots5.domain.ids import Uuid7Factory  # noqa: E402
from bots5.domain.models import (  # noqa: E402
    Message,
    MessageRole,
    MessageState,
    WorkspaceWindowState,
)
from bots5.infrastructure.generation.fake import FakeStreamingBackend  # noqa: E402
from tests._authority_test_support import (  # noqa: E402
    SQLiteAppStateStore,
    upgrade_database,
)


def _run_qasync(qt_application: QApplication, operation) -> None:
    """Run a desktop scenario on a qasync loop tied to this test."""
    qt_application.setQuitOnLastWindowClosed(False)
    event_loop = QEventLoop(qt_application)
    asyncio.set_event_loop(event_loop)
    with event_loop:
        event_loop.run_until_complete(operation)


def _desktop_application(tmp_path: Path) -> tuple[BotsApplication, Path]:
    """A real BotsApplication over the real SQLite workspace/settings plane."""
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    ids = Uuid7Factory()
    clock = SystemClock()
    application = BotsApplication(
        SQLiteAppStateStore.open(database),
        EventBus(clock, ids, queue_size=32),
        FakeStreamingBackend(),
        ids=ids,
        clock=clock,
    )
    return application, database


async def _wait_until(predicate, *, timeout: float = 4.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for Phase 11 desktop state")
        await asyncio.sleep(0.01)


async def _flush() -> None:
    """Let scheduled desktop tasks run and Qt layout events settle.

    Deliberately does NOT call QApplication.processEvents(): stepping Qt
    events inside a running asyncio task re-enters the qasync loop while
    another task is executing, which loses task wakeups on Python 3.14.
    Plain awaits let the integrated loop interleave both worlds safely.
    """
    for _ in range(8):
        await asyncio.sleep(0.01)


async def _dispose_window(window: MainWindow) -> None:
    """Tear one window down through its real close path and release the
    widget tree, so these desktop tests leave no Qt residue behind for the
    (single shared QApplication) tests that run later in the suite."""
    try:
        if window._window_id is not None:
            await window._finish_close()
        else:
            window.stop_bridge()
    finally:
        window.deleteLater()
        for _ in range(6):
            await asyncio.sleep(0.01)


def _window_state(window_id: str, **overrides) -> WorkspaceWindowState:
    kwargs = dict(
        window_id=window_id,
        ordinal=0,
        geometry=None,
        selected_chat_id=None,
        rail_collapsed=False,
        restore_open=True,
        updated_at=datetime.now(UTC),
    )
    kwargs.update(overrides)
    return WorkspaceWindowState(**kwargs)


def _transcript_messages(chat_id: str) -> tuple[Message, ...]:
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    messages: list[Message] = []
    parent: str | None = None
    for index in range(1, 21):
        user = index % 2 == 1
        message = Message(
            id=f"scroll-message-{index}",
            chat_id=chat_id,
            role=MessageRole.USER if user else MessageRole.ASSISTANT,
            state=MessageState.SENT if user else MessageState.COMPLETE,
            content=f"scroll filler {index}: " + "lorem ipsum dolor sit amet " * 8,
            sequence=index,
            created_at=now,
            parent_id=parent,
        )
        parent = message.id
        messages.append(message)
    return tuple(messages)


def _upgrade_to_revision(database: Path, revision: str) -> None:
    """Upgrade database to specified revision using raw alembic."""
    from tests._authority_test_support import upgrade_to
    upgrade_to(database, revision)


class TestWorkspaceStateSchema:
    """Tests for the Phase 11 workspace state schema."""

    def test_migration_creates_maximized_and_scroll_columns(self, tmp_path: Path):
        """Test 1: Migration adds maximized and transcript_scroll_position columns."""
        database = tmp_path / "state.sqlite3"
        _upgrade_to_revision(database, HEAD)

        conn = sqlite3.connect(database)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(workspace_windows)")
            columns = {row[1] for row in cursor.fetchall()}

            assert "maximized" in columns, (
                "workspace_windows missing maximized column"
            )
            assert "transcript_scroll_position" in columns, (
                "workspace_windows missing transcript_scroll_position column"
            )
        finally:
            conn.close()

    def test_migration_creates_chat_drafts_table(self, tmp_path: Path):
        """Test 2: Migration creates chat_drafts table with correct schema."""
        database = tmp_path / "state.sqlite3"
        _upgrade_to_revision(database, HEAD)

        conn = sqlite3.connect(database)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(chat_drafts)")
            cols = {row[1] for row in cursor.fetchall()}
            assert cols == {"chat_id", "draft_text", "updated_at"}, (
                f"chat_drafts table has wrong columns: {cols}"
            )

            # chat_drafts MUST keep its FK to chats so a deleted chat cannot orphan a draft.
            cursor.execute("PRAGMA foreign_key_list(chat_drafts)")
            fks = cursor.fetchall()
            assert len(fks) == 1, f"chat_drafts should have exactly one FK, found: {fks}"
            assert fks[0][2] == "chats", f"chat_drafts FK must target chats, found: {fks[0][2]}"
            assert (fks[0][6] or "").upper() == "CASCADE", (
                f"chat_drafts FK must cascade on delete, found: {fks[0][6]}"
            )
        finally:
            conn.close()

    def test_migration_creates_dock_layout_table(self, tmp_path: Path):
        """Test 3: Migration creates dock_layout table for QMainWindow saveState."""
        database = tmp_path / "state.sqlite3"
        _upgrade_to_revision(database, HEAD)

        conn = sqlite3.connect(database)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(dock_layout)")
            cols = {row[1] for row in cursor.fetchall()}
            assert cols == {"window_id", "dock_state_blob", "updated_at"}, (
                f"dock_layout table has wrong columns: {cols}"
            )

            # dock_layout has NO FK to workspace_windows - dock layouts may exist independently
            cursor.execute("PRAGMA foreign_key_list(dock_layout)")
            fks = cursor.fetchall()
            assert len(fks) == 0, f"dock_layout should not have FKs, found: {fks}"
        finally:
            conn.close()

    def test_migration_creates_keybinding_overrides_table(self, tmp_path: Path):
        """Test 4: Migration creates keybinding_overrides table."""
        database = tmp_path / "state.sqlite3"
        _upgrade_to_revision(database, HEAD)

        conn = sqlite3.connect(database)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(keybinding_overrides)")
            cols = {row[1] for row in cursor.fetchall()}
            assert cols == {"action_id", "shortcut", "conflict_detected", "updated_at"}, (
                f"keybinding_overrides has wrong columns: {cols}"
            )
        finally:
            conn.close()

    def test_migration_creates_font_scale_settings_table(self, tmp_path: Path):
        """Test 5: Migration creates font_scale_settings table with single-row constraint."""
        database = tmp_path / "state.sqlite3"
        _upgrade_to_revision(database, HEAD)

        conn = sqlite3.connect(database)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(font_scale_settings)")
            cols = {row[1] for row in cursor.fetchall()}
            assert cols == {
                "id", "scale_factor", "ui_font_family", "transcript_font_family",
                "code_font_family", "base_font_size_pt", "updated_at"
            }, f"font_scale_settings has wrong columns: {cols}"

            # Verify single-row constraint exists
            cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='font_scale_settings'"
            )
            sql = cursor.fetchone()[0]
            assert "ck_font_scale_single_row" in sql, (
                "font_scale_settings missing single-row constraint"
            )
        finally:
            conn.close()

    def test_migration_increments_alembic_version(self, tmp_path: Path):
        """Test 6: Migration sets alembic_version to 0017_phase11_integrity."""
        database = tmp_path / "state.sqlite3"
        _upgrade_to_revision(database, HEAD)

        conn = sqlite3.connect(database)
        try:
            version = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            assert version == HEAD, f"Expected {HEAD}, got {version}"
        finally:
            conn.close()


class TestWorkspaceStatePersistence:
    """Tests for the workspace state persistence via authority store."""

    def test_save_and_load_chat_draft(self, tmp_path: Path):
        """Test 7: Round-trip chat draft persistence through the authority store."""
        data_root = tmp_path / "data_root"
        data_root.mkdir(mode=0o700, exist_ok=True)
        
        from bots5.infrastructure.data_root_authority import DataRootAuthority
        from bots5.infrastructure.persistence.migration_runner import upgrade_database
        from bots5.domain.models import Chat
        from uuid6 import uuid7
        
        authority = DataRootAuthority(data_root).acquire()
        store = authority.open_store()
        try:
            # Create a chat first (required for FK - though we removed it, it's good practice)
            chat_id = str(uuid7())
            now = datetime.now(UTC)
            chat = Chat(chat_id, "Test Chat", now, now)
            with store.command_admission():
                store.create_chat(chat)

            # Save a draft for the chat we actually created (FK-bound).
            with store.command_admission():
                store.save_chat_draft(chat_id, "This is a test draft")

            # Load it back (must be wrapped in command_admission)
            with store.command_admission():
                draft = store.get_chat_draft(chat_id)
            assert draft == "This is a test draft", (
                f"Draft mismatch: expected 'This is a test draft', got '{draft}'"
            )

            # Verify it's actually in SQLite (at the canonical path)
            database = data_root / "database" / "state.sqlite3"
            with sqlite3.connect(database) as conn:
                row = conn.execute(
                    "SELECT draft_text FROM chat_drafts WHERE chat_id=?", (chat_id,)
                ).fetchone()
                assert row is not None, "Draft not in SQLite"
                assert row[0] == "This is a test draft", "Draft content mismatch in SQLite"
        finally:
            store.close()
            authority.close()

    def test_update_chat_draft(self, tmp_path: Path):
        """Test 8: Update existing chat draft."""
        data_root = tmp_path / "data_root"
        data_root.mkdir(mode=0o700, exist_ok=True)
        
        from bots5.infrastructure.data_root_authority import DataRootAuthority
        
        authority = DataRootAuthority(data_root).acquire()
        store = authority.open_store()
        try:
            # A draft is FK-bound to a real chat, so create one first.
            from bots5.domain.models import Chat
            from uuid6 import uuid7

            chat_id = str(uuid7())
            now = datetime.now(UTC)
            with store.command_admission():
                store.create_chat(Chat(chat_id, "Draft Chat", now, now))

            with store.command_admission():
                store.save_chat_draft(chat_id, "First draft")
            with store.command_admission():
                store.save_chat_draft(chat_id, "Updated draft")

            # Load and verify (must be wrapped in command_admission)
            with store.command_admission():
                draft = store.get_chat_draft(chat_id)
            assert draft == "Updated draft", f"Draft not updated: {draft}"
        finally:
            store.close()
            authority.close()

    def test_save_and_load_dock_layout(self, tmp_path: Path):
        """Test 9: Round-trip dock layout persistence through the authority store."""
        data_root = tmp_path / "data_root"
        data_root.mkdir(mode=0o700, exist_ok=True)
        
        from bots5.infrastructure.data_root_authority import DataRootAuthority
        
        authority = DataRootAuthority(data_root).acquire()
        store = authority.open_store()
        try:
            # Save a dock layout (simulating QMainWindow.saveState())
            dock_blob = b"fake_qt_dock_state_bytes_12345"
            with store.command_admission():
                store.save_dock_layout("window-abc", dock_blob)

            # Load it back (must be wrapped in command_admission)
            with store.command_admission():
                loaded = store.get_dock_layout("window-abc")
            assert loaded == dock_blob, (
                f"Dock layout mismatch: expected {dock_blob!r}, got {loaded!r}"
            )

            # Verify in SQLite (at the canonical path)
            database = data_root / "database" / "state.sqlite3"
            with sqlite3.connect(database) as conn:
                row = conn.execute(
                    "SELECT dock_state_blob FROM dock_layout WHERE window_id='window-abc'"
                ).fetchone()
                assert row is not None, "Dock layout not in SQLite"
                assert bytes(row[0]) == dock_blob, "Dock layout mismatch in SQLite"
        finally:
            store.close()
            authority.close()

    def test_save_keybinding_override(self, tmp_path: Path):
        """Test 10: Keybinding override persistence through the authority store."""
        data_root = tmp_path / "data_root"
        data_root.mkdir(mode=0o700, exist_ok=True)
        
        from bots5.infrastructure.data_root_authority import DataRootAuthority
        
        authority = DataRootAuthority(data_root).acquire()
        store = authority.open_store()
        try:
            # Save a keybinding override
            with store.command_admission():
                store.save_keybinding_override("chat.create", "Ctrl+Shift+N", conflict_detected=False)

            # Load it back (must be wrapped in command_admission)
            with store.command_admission():
                shortcut = store.get_keybinding_override("chat.create")
            assert shortcut == "Ctrl+Shift+N", f"Keybinding mismatch: {shortcut}"

            # Verify in SQLite (at the canonical path)
            database = data_root / "database" / "state.sqlite3"
            with sqlite3.connect(database) as conn:
                row = conn.execute(
                    "SELECT shortcut, conflict_detected FROM keybinding_overrides "
                    "WHERE action_id='chat.create'"
                ).fetchone()
                assert row is not None, "Keybinding not in SQLite"
                assert row[0] == "Ctrl+Shift+N", "Keybinding mismatch in SQLite"
                assert row[1] == 0, "conflict_detected should be 0"
        finally:
            store.close()
            authority.close()

    def test_keybinding_conflict_detection(self, tmp_path: Path):
        """Test 11: Keybinding conflict detection flag through the authority store."""
        data_root = tmp_path / "data_root"
        data_root.mkdir(mode=0o700, exist_ok=True)
        
        from bots5.infrastructure.data_root_authority import DataRootAuthority
        
        authority = DataRootAuthority(data_root).acquire()
        store = authority.open_store()
        try:
            # Save with conflict flag
            with store.command_admission():
                store.save_keybinding_override(
                    "chat.create", "Ctrl+Shift+N", conflict_detected=True
                )

            # Load and verify conflict flag (must be wrapped in command_admission)
            with store.command_admission():
                shortcut = store.get_keybinding_override("chat.create")
            assert shortcut == "Ctrl+Shift+N"

            # Verify in SQLite (at the canonical path)
            database = data_root / "database" / "state.sqlite3"
            with sqlite3.connect(database) as conn:
                row = conn.execute(
                    "SELECT conflict_detected FROM keybinding_overrides "
                    "WHERE action_id='chat.create'"
                ).fetchone()
                assert row[0] == 1, "conflict_detected should be 1 (True)"
        finally:
            store.close()
            authority.close()

    def test_reset_keybinding_overrides(self, tmp_path: Path):
        """Test 12: Reset all keybinding overrides through the authority store."""
        data_root = tmp_path / "data_root"
        data_root.mkdir(mode=0o700, exist_ok=True)
        
        from bots5.infrastructure.data_root_authority import DataRootAuthority
        
        authority = DataRootAuthority(data_root).acquire()
        store = authority.open_store()
        try:
            # Save some overrides
            with store.command_admission():
                store.save_keybinding_override("action1", "Ctrl+A")
                store.save_keybinding_override("action2", "Ctrl+B")

            # Reset
            with store.command_admission():
                store.reset_keybinding_overrides()

            # Verify they're gone (must be wrapped in command_admission)
            with store.command_admission():
                assert store.get_keybinding_override("action1") == ""
                assert store.get_keybinding_override("action2") == ""

            # Verify table is empty
            database = data_root / "database" / "state.sqlite3"
            with sqlite3.connect(database) as conn:
                count = conn.execute(
                    "SELECT count(*) FROM keybinding_overrides"
                ).fetchone()[0]
                assert count == 0, f"Expected 0 overrides, got {count}"
        finally:
            store.close()
            authority.close()

    def test_save_and_load_font_scale_settings(self, tmp_path: Path):
        """Test 13: Font/scale settings persistence through the authority store."""
        data_root = tmp_path / "data_root"
        data_root.mkdir(mode=0o700, exist_ok=True)
        
        from bots5.infrastructure.data_root_authority import DataRootAuthority
        
        authority = DataRootAuthority(data_root).acquire()
        store = authority.open_store()
        try:
            # Save settings
            with store.command_admission():
                store.save_font_scale_settings(
                    scale_factor=1.25,
                    ui_font_family="Inter",
                    transcript_font_family="Georgia",
                    code_font_family="Fira Code",
                    base_font_size_pt=12.0,
                )

            # Load back (must be wrapped in command_admission)
            with store.command_admission():
                settings = store.get_font_scale_settings()
            assert settings[0] == 1.25, f"Scale mismatch: {settings[0]}"
            assert settings[1] == "Inter", f"UI font mismatch: {settings[1]}"
            assert settings[2] == "Georgia", f"Transcript font mismatch: {settings[2]}"
            assert settings[3] == "Fira Code", f"Code font mismatch: {settings[3]}"
            assert settings[4] == 12.0, f"Font size mismatch: {settings[4]}"

            # Verify in SQLite (at the canonical path)
            database = data_root / "database" / "state.sqlite3"
            with sqlite3.connect(database) as conn:
                row = conn.execute(
                    "SELECT scale_factor, ui_font_family, transcript_font_family, "
                    "code_font_family, base_font_size_pt FROM font_scale_settings"
                ).fetchone()
                assert row is not None, "Font settings not in SQLite"
                assert row[0] == 1.25, "Scale mismatch in SQLite"
        finally:
            store.close()
            authority.close()

    def test_single_row_constraint_on_font_scale_settings(self, tmp_path: Path):
        """Test 14: Only one row allowed in font_scale_settings."""
        data_root = tmp_path / "data_root"
        data_root.mkdir(mode=0o700, exist_ok=True)
        
        from bots5.infrastructure.data_root_authority import DataRootAuthority
        
        authority = DataRootAuthority(data_root).acquire()
        store = authority.open_store()
        try:
            # Save first row
            with store.command_admission():
                store.save_font_scale_settings(
                    scale_factor=1.0, ui_font_family="UI1", transcript_font_family="T1",
                    code_font_family="C1", base_font_size_pt=10.0
                )

            # Save again - should update, not create new row
            with store.command_admission():
                store.save_font_scale_settings(
                    scale_factor=1.5, ui_font_family="UI2", transcript_font_family="T2",
                    code_font_family="C2", base_font_size_pt=12.0
                )

            # Verify only one row exists
            database = data_root / "database" / "state.sqlite3"
            with sqlite3.connect(database) as conn:
                count = conn.execute(
                    "SELECT count(*) FROM font_scale_settings"
                ).fetchone()[0]
                assert count == 1, f"Expected 1 row, got {count}"

                # Verify it's the updated values
                row = conn.execute(
                    "SELECT scale_factor, ui_font_family FROM font_scale_settings"
                ).fetchone()
                assert row[0] == 1.5, "Row not updated"
                assert row[1] == "UI2", "Row not updated"
        finally:
            store.close()
            authority.close()


class TestDowngrade:
    """Tests for migration downgrade."""

    def test_downgrade_removes_tables_and_columns(self, tmp_path: Path):
        """Test 15: Downgrade to prior revision removes Phase 11 M4b/M6 additions."""
        database = tmp_path / "state.sqlite3"
        _upgrade_to_revision(database, HEAD)

        # Verify new schema exists
        with sqlite3.connect(database) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(workspace_windows)")
            cols = {row[1] for row in cursor.fetchall()}
            assert "maximized" in cols
            assert "transcript_scroll_position" in cols

        # Downgrade to prior revision
        from tests._authority_test_support import downgrade_to
        downgrade_to(database, PRIOR_HEAD)

        # Verify new schema removed
        with sqlite3.connect(database) as conn:
            cursor = conn.cursor()

            # Columns should be gone
            cursor.execute("PRAGMA table_info(workspace_windows)")
            cols = {row[1] for row in cursor.fetchall()}
            assert "maximized" not in cols, "maximized should be removed"
            assert "transcript_scroll_position" not in cols, "transcript_scroll_position should be removed"

            # Tables should be gone
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('chat_drafts', 'dock_layout', 'keybinding_overrides', 'font_scale_settings')"
            )
            tables = {row[0] for row in cursor.fetchall()}
            assert tables == set(), f"Phase 11 tables should be removed: {tables}"


class TestFreshDatabase:
    """Tests for fresh database migrations."""

    def test_fresh_database_reaches_head_revision(self, tmp_path: Path):
        """Test 16: Fresh database migrates to 0017_phase11_integrity."""
        data_root = tmp_path / "data_root"
        data_root.mkdir(mode=0o700, exist_ok=True)

        # Open with authority which should run migrations
        from bots5.infrastructure.data_root_authority import DataRootAuthority
        authority = DataRootAuthority(data_root).acquire()
        store = authority.open_store()
        try:
            database = data_root / "database" / "state.sqlite3"
            with sqlite3.connect(database) as conn:
                version = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()[0]
                assert version == HEAD, f"Expected {HEAD}, got {version}"
        finally:
            store.close()
            authority.close()

    def test_downgrade_of_0016_restores_prior_schema(self, tmp_path: Path):
        """Test 17: Downgrade of 0016 to 0015 removes new tables/columns."""
        database = tmp_path / "state.sqlite3"
        _upgrade_to_revision(database, HEAD)

        # Verify 0016 schema exists
        with sqlite3.connect(database) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(workspace_windows)")
            cols = {row[1] for row in cursor.fetchall()}
            assert "maximized" in cols
            assert "transcript_scroll_position" in cols

            # Verify all new tables exist
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('chat_drafts', 'dock_layout', 'keybinding_overrides', 'font_scale_settings')"
            )
            tables = {row[0] for row in cursor.fetchall()}
            assert len(tables) == 4, f"Expected 4 tables, got {len(tables)}: {tables}"

        # Downgrade to 0015
        from tests._authority_test_support import downgrade_to
        downgrade_to(database, PRIOR_HEAD)

        # Verify schema restored to 0015
        with sqlite3.connect(database) as conn:
            version = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            assert version == PRIOR_HEAD, f"Expected {PRIOR_HEAD}, got {version}"

            # Columns should be gone
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(workspace_windows)")
            cols = {row[1] for row in cursor.fetchall()}
            assert "maximized" not in cols, "maximized should be removed after downgrade"
            assert "transcript_scroll_position" not in cols, "transcript_scroll_position should be removed after downgrade"

            # New tables should be gone
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('chat_drafts', 'dock_layout', 'keybinding_overrides', 'font_scale_settings')"
            )
            tables = {row[0] for row in cursor.fetchall()}
            assert tables == set(), f"Phase 11 tables should be removed: {tables}"


class TestDesktopRestorationWiring:
    """Phase 11 M0a/M4b/M6: the desktop must genuinely read and write the
    workspace/settings plane through real windows.

    Every test below is feature-positive: it drives a real MainWindow through
    the real persistence plane and asserts a behaviour that FAILS if the
    desktop wiring is absent (reverted to the pre-wiring code).
    """

    # ------------------------------------------------------------------
    # M0a: geometry clamping + maximized state (feature: F10)
    # ------------------------------------------------------------------

    def test_restored_off_screen_geometry_is_clamped_preserving_stored_size(
        self, tmp_path: Path
    ):
        """Test 18: an off-screen stored geometry is clamped on-screen with its
        stored size preserved (the fence U-10 defect discarded the size)."""
        qt_application = QApplication.instance() or QApplication([])

        async def scenario() -> None:
            application, database = _desktop_application(tmp_path)
            screens = QGuiApplication.screens()
            assert screens, "offscreen platform must expose at least one screen"
            union = screens[0].availableGeometry()
            for screen in screens[1:]:
                union = union.united(screen.availableGeometry())
            stored = (
                union.right() + 20_000,
                union.bottom() + 20_000,
                460,
                320,
            )
            # The helper itself must preserve the stored size off-union.
            clamped = clamp_geometry_to_available_screens(stored)
            assert clamped[2:] == (460, 320)
            assert union.left() <= clamped[0] <= union.right() - 460 + 1
            assert union.top() <= clamped[1] <= union.bottom() - 320 + 1

            state = _window_state("window-clamp", geometry=stored)
            window = MainWindow(application, window_state=state)
            try:
                await window.initialize()
                geometry = window.geometry()
                # FEATURE-POSITIVE clamping: the pre-M0a code applied the
                # stored geometry verbatim, leaving the window off-screen.
                assert union.left() <= geometry.x(), (
                    f"restored x not clamped on-screen: {geometry.x()}"
                )
                assert union.top() <= geometry.y(), (
                    f"restored y not clamped on-screen: {geometry.y()}"
                )
                assert geometry.x() + geometry.width() - 1 <= union.right()
                assert geometry.y() + geometry.height() - 1 <= union.bottom()
                # Fence U-10 regression guard: the stored SIZE is preserved.
                assert (geometry.width(), geometry.height()) == (460, 320), (
                    "restored size must be preserved, not discarded"
                )
                window.show()
                await asyncio.sleep(0.01)
                geometry = window.geometry()
                assert union.left() <= geometry.x(), "shown window fell off-screen (x)"
                assert union.top() <= geometry.y(), "shown window fell off-screen (y)"
                # FEATURE-POSITIVE persistence: the clamped geometry and the
                # non-maximized flag reached SQLite through the store methods.
                with sqlite3.connect(database) as conn:
                    row = conn.execute(
                        "SELECT maximized, geometry_json FROM workspace_windows "
                        "WHERE window_id='window-clamp'"
                    ).fetchone()
                assert row is not None, "workspace window row missing from SQLite"
                assert row[0] == 0, "window must not be flagged maximized"
                persisted = tuple(json.loads(row[1]))
                assert persisted[2:] == (460, 320), (
                    f"persisted geometry lost the stored size: {persisted}"
                )
                assert union.left() <= persisted[0] <= union.right() - 460 + 1
                assert union.top() <= persisted[1] <= union.bottom() - 320 + 1
            finally:
                await _dispose_window(window)
                await application.close()

        _run_qasync(qt_application, scenario())

    def test_maximized_state_round_trips_through_window_and_sqlite(
        self, tmp_path: Path
    ):
        """Test 19: a persisted maximized flag is applied at startup, saved
        back through the store methods, and restored into a rebuilt window."""
        qt_application = QApplication.instance() or QApplication([])

        async def scenario() -> None:
            application, database = _desktop_application(tmp_path)
            state = _window_state("window-max", maximized=True)
            window = MainWindow(application, window_state=state)
            try:
                await window.initialize()
                # FEATURE-POSITIVE maximized restore: without the wiring the
                # flag is never applied and isMaximized() stays False.
                assert window.isMaximized(), "persisted maximized state not applied"
                window.show()
                await asyncio.sleep(0.01)
                assert window.isMaximized()
                # FEATURE-POSITIVE persistence: maximized reached SQLite through
                # the store methods.
                with sqlite3.connect(database) as conn:
                    row = conn.execute(
                        "SELECT maximized FROM workspace_windows "
                        "WHERE window_id='window-max'"
                    ).fetchone()
                assert row is not None, "workspace window row missing from SQLite"
                assert row[0] == 1, "maximized not persisted in SQLite"
            finally:
                await _dispose_window(window)

            states = await application.list_workspace_windows()
            state = next(item for item in states if item.window_id == "window-max")
            assert state.maximized is True, (
                "reloaded workspace state lost the maximized flag"
            )
            rebuilt = MainWindow(application, window_state=state)
            try:
                await rebuilt.initialize()
                rebuilt.show()
                await asyncio.sleep(0.01)
                assert rebuilt.isMaximized(), (
                    "rebuilt window did not restore the maximized state"
                )
            finally:
                await _dispose_window(rebuilt)
                await application.close()

        _run_qasync(qt_application, scenario())

    # ------------------------------------------------------------------
    # M0a/M4b: restore-aware transcript scrolling
    # ------------------------------------------------------------------

    def test_transcript_scroll_position_round_trips_through_windows_and_sqlite(
        self, tmp_path: Path
    ):
        """Test 19: the transcript scroll position is persisted on save, is
        present in SQLite, and a rebuilt window restores THAT position instead
        of unconditionally jumping to the bottom."""
        qt_application = QApplication.instance() or QApplication([])

        async def scenario() -> None:
            application, database = _desktop_application(tmp_path)
            window = MainWindow(application)
            target: int
            try:
                await window.initialize()
                chat_id = window._current_chat_id
                window.show()
                window.resize(420, 300)
                await _flush()
                window.transcript.render(_transcript_messages(chat_id))
                await _flush()
                bar = window.transcript.verticalScrollBar()
                assert bar.maximum() >= 20, "precondition: transcript must scroll"
                # A mid-range target: reachable, but far from both edges so a
                # restore-to-bottom or restore-to-top cannot fake a pass.
                target = bar.maximum() // 2
                bar.setValue(target)
                await _flush()
                assert bar.value() == target, "scrollbar did not hold the target"
                assert window.transcript.scroll_position == target
                await window._save_workspace()
                # FEATURE-POSITIVE persistence: the scroll position is in SQLite.
                with sqlite3.connect(database) as conn:
                    row = conn.execute(
                        "SELECT transcript_scroll_position, maximized "
                        "FROM workspace_windows WHERE window_id=?",
                        (window._window_id,),
                    ).fetchone()
                assert row is not None, "workspace window row missing from SQLite"
                assert row[0] == target, (
                    f"scroll position missing from SQLite (got {row[0]!r})"
                )
                await window._finish_close()

                # The real restore path reads it back from the store.
                states = await application.list_workspace_windows()
                assert len(states) == 1
                assert states[0].transcript_scroll_position == target
            finally:
                await _dispose_window(window)

            restored_state = states[0]
            rebuilt = MainWindow(application, window_state=restored_state)
            try:
                await rebuilt.initialize()
                # The empty rebuilt transcript cannot represent the target yet,
                # so the restore must still be pending (not silently dropped).
                assert rebuilt.transcript.pending_restore_position == target, (
                    "rebuilt window dropped the persisted scroll position"
                )
                rebuilt.show()
                rebuilt.resize(420, 300)
                await _flush()
                rebuilt.transcript.render(_transcript_messages(chat_id))
                await _flush()
                bar = rebuilt.transcript.verticalScrollBar()
                assert bar.maximum() >= target, "precondition: rebuilt content scrolls"
                # FEATURE-POSITIVE restore-aware scrolling: the pre-M0a code
                # ends every render with setValue(maximum()), i.e. the bottom.
                assert bar.value() == target, (
                    f"rebuilt window did not restore scroll position {target} "
                    f"(got {bar.value()})"
                )
            finally:
                await _dispose_window(rebuilt)
                await application.close()

        _run_qasync(qt_application, scenario())

    # ------------------------------------------------------------------
    # M4b: chat draft persistence
    # ------------------------------------------------------------------

    def test_chat_draft_round_trips_through_composer_switch_reopen_and_sqlite(
        self, tmp_path: Path
    ):
        """Test 20: switching chats stores the outgoing composer text as the
        outgoing chat's draft in SQLite, and reopening the chat restores it."""
        qt_application = QApplication.instance() or QApplication([])

        async def scenario() -> None:
            application, database = _desktop_application(tmp_path)
            window = MainWindow(application)
            try:
                await window.initialize()
                first_chat_id = window._current_chat_id
                assert first_chat_id is not None
                await application.create_chat(title="Second chat")
                window._replace_chat_list(await application.list_chats())

                window.composer.setPlainText("draft alpha for chat one")
                second_chat_id = next(
                    chat.id for chat in await application.list_chats()
                    if chat.id != first_chat_id
                )
                window._on_chat_selected(window._chat_ids.index(second_chat_id))
                # Let the scheduled draft switch run to completion, then read
                # SQLite once (no concurrent reader polling while it writes).
                await _flush()
                # Draft-1 contract preserved: with no stored draft for the
                # incoming chat, in-progress composer text is left untouched.
                assert window.composer.toPlainText() == "draft alpha for chat one", (
                    "composer text must survive a switch to a draft-less chat"
                )
                # FEATURE-POSITIVE persistence: the outgoing chat's draft is in
                # SQLite through the store methods.
                stored = _query_scalar(
                    database,
                    "SELECT draft_text FROM chat_drafts WHERE chat_id=?",
                    (first_chat_id,),
                )
                assert stored == "draft alpha for chat one", (
                    f"draft not persisted for the outgoing chat (got {stored!r})"
                )
                # Switching back restores the persisted draft into the composer.
                window._on_chat_selected(window._chat_ids.index(first_chat_id))
                await _flush()
                assert window.composer.toPlainText() == "draft alpha for chat one", (
                    "switching back did not restore the persisted draft"
                )
            finally:
                await _dispose_window(window)

            state = _window_state("window-draft", selected_chat_id=first_chat_id)
            reopened = MainWindow(application, window_state=state)
            try:
                await reopened.initialize()
                # FEATURE-POSITIVE restore: a rebuilt window shows the stored
                # draft in the composer.
                assert reopened.composer.toPlainText() == "draft alpha for chat one", (
                    "reopened chat did not restore its persisted draft"
                )
            finally:
                await _dispose_window(reopened)
                await application.close()

        _run_qasync(qt_application, scenario())

    # ------------------------------------------------------------------
    # M4b: dock layout blob (advisory, malformed-safe)
    # ------------------------------------------------------------------

    def test_dock_layout_blob_round_trips_and_malformed_blob_falls_back(
        self, tmp_path: Path
    ):
        """Test 21: a real saveState() blob is persisted and restores the dock
        visibility in a rebuilt window, while a malformed advisory blob falls
        back to the default layout without raising."""
        qt_application = QApplication.instance() or QApplication([])

        async def scenario() -> None:
            application, database = _desktop_application(tmp_path)
            window = MainWindow(application)
            try:
                await window.initialize()
                window.show()
                await asyncio.sleep(0.01)
                window.search_dock.show()
                await asyncio.sleep(0.01)
                await _flush()
                window_id = window._window_id
                # Let the visibility-triggered persistence run to completion,
                # then read SQLite once (no reader polling while it writes).
                await _flush()
                # FEATURE-POSITIVE persistence: the real saveState() blob is in
                # SQLite through the store methods.
                stored = _query_row(
                    database,
                    "SELECT dock_state_blob FROM dock_layout WHERE window_id=?",
                    (window_id,),
                )
                assert stored is not None, "dock layout blob missing from SQLite"
                assert bytes(stored[0]) == bytes(window.saveState()), (
                    "persisted dock blob is not the window's saveState() payload"
                )
            finally:
                await _dispose_window(window)

            states = await application.list_workspace_windows()
            state = next(item for item in states if item.window_id == window_id)
            rebuilt = MainWindow(application, window_state=state)
            try:
                await rebuilt.initialize()
                rebuilt.show()
                # FEATURE-POSITIVE restore: without the wiring the search dock
                # stays hidden in the rebuilt window. Poll at a fine interval so a
                # scheduling delay under load cannot masquerade as a failure; the
                # assertion itself is unchanged.
                for _ in range(500):
                    if rebuilt.search_dock.isVisible():
                        break
                    await asyncio.sleep(0.005)
                assert rebuilt.search_dock.isVisible(), (
                    "rebuilt window did not restore the dock layout from the blob"
                )
            finally:
                await _dispose_window(rebuilt)

            # Malformed advisory blob: must fall back to the default layout
            # (mirroring the store's malformed-row fallback) WITHOUT raising.
            await application.save_dock_layout(
                "window-malformed", b"\x00\x01not-a-qt-dock-blob"
            )
            malformed = MainWindow(
                application,
                window_state=_window_state("window-malformed"),
            )
            try:
                # The very construction below must not raise.
                await malformed.initialize()
                malformed.show()
                # Wait deterministically for the fallback to be both applied and
                # REPORTED. The status-bar message is shown with a 5s timeout, so
                # this must poll at a FINE interval: using _flush() per iteration
                # (80ms) would burn the whole 5s window and then read an expired
                # message as empty. Poll on the condition directly instead, and
                # no assertion is relaxed.
                for _ in range(500):
                    if (
                        malformed.search_dock.isHidden()
                        and "incompatible"
                        in malformed.statusBar().currentMessage()
                    ):
                        break
                    await asyncio.sleep(0.005)
                assert malformed.search_dock.isHidden(), (
                    "malformed dock blob must fall back to the default layout"
                )
                assert "incompatible" in malformed.statusBar().currentMessage(), (
                    "malformed dock blob fallback was not reported"
                )
            finally:
                await _dispose_window(malformed)
                await application.close()

        _run_qasync(qt_application, scenario())

    # ------------------------------------------------------------------
    # M6: keybinding overrides (apply at startup, conflict, reset)
    # ------------------------------------------------------------------

    def test_keybinding_override_round_trips_with_conflict_detection_and_reset(
        self, tmp_path: Path
    ):
        """Test 22: an override is persisted with conflict detection, is
        re-applied at startup in a rebuilt window, and reset clears the plane."""
        qt_application = QApplication.instance() or QApplication([])

        async def scenario() -> None:
            application, database = _desktop_application(tmp_path)
            window = MainWindow(application)
            try:
                await window.initialize()
                # FEATURE-POSITIVE: the override retargets the REAL QAction and
                # is persisted through the store methods.
                conflict = window.apply_keybinding_override(
                    "view.global_search", "Ctrl+Shift+G"
                )
                assert conflict is False, "first override must not report a conflict"
                # Let the scheduled persistence run, then read SQLite once.
                await _flush()
                assert window.global_search_action.shortcut().toString() == (
                    "Ctrl+Shift+G"
                ), "override was not applied to the user-visible QAction"
                row = _query_row(
                    database,
                    "SELECT shortcut, conflict_detected FROM keybinding_overrides "
                    "WHERE action_id='view.global_search'",
                    (),
                )
                assert row == ("Ctrl+Shift+G", 0), f"unexpected override row: {row}"

                # Conflict detection: a second action claiming the same chord is
                # reported AND recorded with conflict_detected=1.
                conflict = window.apply_keybinding_override(
                    "view.search_current_chat", "Ctrl+Shift+G"
                )
                assert conflict is True, "duplicate chord must be reported as a conflict"
                await _flush()
                conflict_row = _query_row(
                    database,
                    "SELECT shortcut, conflict_detected FROM keybinding_overrides "
                    "WHERE action_id='view.search_current_chat'",
                    (),
                )
                assert conflict_row == ("Ctrl+Shift+G", 1), (
                    f"conflicting override not persisted with its flag: {conflict_row}"
                )
            finally:
                await _dispose_window(window)

            rebuilt = MainWindow(application)
            try:
                await rebuilt.initialize()
                # FEATURE-POSITIVE startup application: without the wiring the
                # rebuilt window keeps the default Ctrl+K chord.
                assert rebuilt.global_search_action.shortcut().toString() == (
                    "Ctrl+Shift+G"
                ), "saved override was not applied at startup"
                assert (
                    rebuilt._action_registry.effective_shortcut("view.global_search")
                    == "Ctrl+Shift+G"
                )
                # Reset restores defaults everywhere and clears the plane.
                rebuilt.reset_keybinding_overrides()
                await _flush()
                assert rebuilt.global_search_action.shortcut().toString() == "Ctrl+K", (
                    "reset did not restore the default shortcut"
                )
                count = _query_scalar(
                    database, "SELECT count(*) FROM keybinding_overrides", ()
                )
                assert count == 0, (
                    f"reset must clear keybinding_overrides in SQLite (got {count})"
                )
            finally:
                await _dispose_window(rebuilt)
                await application.close()

        _run_qasync(qt_application, scenario())

    # ------------------------------------------------------------------
    # M6/F9: font/scale settings applied on startup
    # ------------------------------------------------------------------

    def test_font_scale_settings_are_applied_on_startup(self, tmp_path: Path):
        """Test 23: persisted font/scale settings are applied to the shell when
        the window starts, with the persisted row present in SQLite."""
        qt_application = QApplication.instance() or QApplication([])

        async def scenario() -> None:
            application, database = _desktop_application(tmp_path)
            try:
                await application.save_font_scale_settings(
                    scale_factor=1.25,
                    ui_font_family=None,
                    transcript_font_family=None,
                    code_font_family=None,
                    base_font_size_pt=11.0,
                )
                row = _query_row(
                    database, "SELECT scale_factor FROM font_scale_settings", ()
                )
                assert row == (1.25,), f"settings row missing from SQLite: {row}"
                window = MainWindow(application)
                baseline = qt_application.styleSheet()
                assert "font-size: 13px" in baseline, (
                    "precondition: default shell must carry the scale-1.0 metrics"
                )
                try:
                    await window.initialize()
                    # FEATURE-POSITIVE: scale 1.25 swaps the 13px base metric for
                    # the 16px scaled one; without the wiring the stylesheet the
                    # window built at construction (13px) would still be active.
                    assert "font-size: 16px" in qt_application.styleSheet(), (
                        "persisted font scale was not applied at startup"
                    )
                finally:
                    await _dispose_window(window)
            finally:
                qt_application.setStyleSheet(build_theme_stylesheet())
                await application.close()

        _run_qasync(qt_application, scenario())


def _query_scalar(database: Path, sql: str, parameters: tuple) -> object:
    with sqlite3.connect(database) as conn:
        row = conn.execute(sql, parameters).fetchone()
        return None if row is None else row[0]


def _query_row(database: Path, sql: str, parameters: tuple) -> tuple | None:
    with sqlite3.connect(database) as conn:
        return conn.execute(sql, parameters).fetchone()
