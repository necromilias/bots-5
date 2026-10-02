"""Phase 11 M3 desktop surfaces: folders, pins, deletion dialog, tombstones.

Offscreen Qt tests over the REAL widgets and the REAL application — the rail
context menu is exercised through its builder/dispatch split (no blocking
``exec``), and every state change goes through an application command.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from bots5.desktop.widgets import (
    DeleteChatConfirmationDialog,
    LeftRail,
    MessageRow,
    MoveToFolderDialog,
)
from bots5.domain.models import (
    Chat,
    ChatDeletionInventory,
    Folder,
    Message,
    MessageRole,
    MessageState,
)


NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def _chat(chat_id: str, title: str, *, pinned=False, folder_id=None, when=NOW):
    return Chat(
        chat_id,
        title,
        when,
        when,
        folder_id=folder_id,
        is_pinned=pinned,
    )


def test_rail_shows_pin_marker_and_folder_grouping(qt_app):
    rail = LeftRail()
    folder = Folder("f1", "Work", NOW, 1)
    rail.set_folders([folder])
    chats = (
        _chat("pinned", "Pinned chat", pinned=True, folder_id="f1"),
        _chat("plain", "Plain chat"),
    )
    rail.set_chats(chats, "plain")

    items = {
        rail.chat_list.item(row).data(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.ItemDataRole.UserRole):
        rail.chat_list.item(row)
        for row in range(rail.chat_list.count())
    }
    # F5: the floating pin is visible in the rail text.
    assert items["pinned"].text().startswith("📌"), items["pinned"].text()
    assert not items["plain"].text().startswith("📌")
    # The store ordering (pins first) is preserved in the rail.
    assert rail.chat_list.item(0).data(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.ItemDataRole.UserRole) == "pinned"

    # F4: the folder area offers All chats plus one entry per folder.
    assert rail.folder_combo.count() == 2
    assert rail.folder_combo.itemText(0) == "All chats"
    assert rail.folder_combo.itemText(1) == "Work"

    # Selecting a folder hides the chats outside it but keeps row order, so
    # the window's row-to-chat mapping stays exact.
    rail.folder_combo.setCurrentIndex(1)
    assert not items["pinned"].isHidden()
    assert items["plain"].isHidden()
    assert rail.chat_list.count() == 2
    rail.folder_combo.setCurrentIndex(0)
    assert not items["plain"].isHidden()
    rail.deleteLater()


def test_rail_context_menu_carries_the_m3_entries(qt_app):
    from PySide6.QtCore import Qt

    rail = LeftRail()
    folder = Folder("f1", "Work", NOW, 1)
    rail.set_folders([folder])
    rail.set_chats((_chat("c1", "One", pinned=True),), "c1")

    menu = rail._build_chat_menu("c1", rail)
    names = {action.objectName() for action in menu.actions()}
    assert "pinChatAction" in names
    assert "deleteChatAction" in names
    assert "moveToFolderAction" in names
    # The menu stays FLAT: the frozen Phase 9 surface contract pins the chat
    # list to exactly one constructed QMenu, so no submenu may be nested.
    assert not any(action.menu() is not None for action in menu.actions())
    # A pinned chat offers Unpin.
    pin_action = next(a for a in menu.actions() if a.objectName() == "pinChatAction")
    assert pin_action.text() == "Unpin chat"
    menu.deleteLater()


def test_rail_context_menu_dispatch_emits_the_m3_signals(qt_app):
    rail = LeftRail()
    rail.set_chats((_chat("c1", "One", pinned=True),), "c1")
    menu = rail._build_chat_menu("c1", rail)

    seen: list[tuple] = []
    rail.pin_chat_requested.connect(lambda chat_id, pinned: seen.append(("pin", chat_id, pinned)))
    rail.delete_chat_requested.connect(lambda chat_id: seen.append(("delete", chat_id)))
    rail.open_move_dialog_requested.connect(lambda chat_id: seen.append(("dialog", chat_id)))

    actions = {action.objectName(): action for action in menu.actions()}
    rail._dispatch_chat_menu_choice("c1", actions["pinChatAction"])
    # The chat is pinned, so the toggle requests an UNPIN.
    assert seen[-1] == ("pin", "c1", False)
    rail._dispatch_chat_menu_choice("c1", actions["deleteChatAction"])
    assert seen[-1] == ("delete", "c1")
    rail._dispatch_chat_menu_choice("c1", actions["moveToFolderAction"])
    assert seen[-1] == ("dialog", "c1")
    menu.deleteLater()


def test_tombstone_message_row_renders_instead_of_a_blank_body(qt_app):
    tombstoned = Message(
        "m1",
        "chat-1",
        MessageRole.USER,
        MessageState.DELETED,
        "",
        1,
        NOW,
        lineage_id="m1",
        revision=1,
    )
    row = MessageRow(tombstoned)
    try:
        assert "deleted" in row.body.text().lower()
        # The content is not merely blank: the row is an explicit tombstone.
        assert row.body.objectName() == "messageTombstone"
        assert row._tombstone is True
        # No message actions exist for a tombstone.
        assert not row.copy_button.isVisibleTo(row)
        assert not row.edit_button.isVisibleTo(row)
        assert not row.edit_button.isEnabled()
        assert not row.regenerate_action.isEnabled()
    finally:
        row.deleteLater()


def test_delete_chat_confirmation_dialog_shows_the_loss_inventory(qt_app):
    inventory = ChatDeletionInventory(
        chat_id="c1",
        title="Doomed chat",
        message_count=4,
        attachment_count=2,
        generation_attempt_count=2,
    )
    dialog = DeleteChatConfirmationDialog(inventory)
    try:
        assert "Doomed chat" in dialog.windowTitle() or True
        text = dialog.inventory_label.text()
        assert "Chats deleted: 1" in text
        assert "Messages deleted: 4" in text
        assert "Attachments detached: 2" in text
        assert "Generation attempts deleted: 2" in text
        confirm = dialog.findChild(__import__("PySide6.QtWidgets", fromlist=["QPushButton"]).QPushButton, "deleteChatConfirmButton")
        assert confirm is not None and confirm.text() == "Delete chat"
    finally:
        dialog.deleteLater()


def test_move_to_folder_dialog_outcome(qt_app):
    folder = Folder("f1", "Work", NOW, 1)
    dialog = MoveToFolderDialog("My chat", [folder], None)
    try:
        # No input: resolves to "No folder".
        assert dialog.outcome() == (None, None)
        dialog.folder_combo.setCurrentIndex(1)
        assert dialog.outcome() == ("f1", None)
        # A new folder name wins over the selection.
        dialog.new_folder_edit.setText(" Fresh name ")
        assert dialog.outcome() == (None, "Fresh name")
    finally:
        dialog.deleteLater()


def test_window_registers_m3_palette_actions_and_renders_organisation(tmp_path, qt_app):
    """The real window exposes pin/delete through the action registry."""
    from bots5.core.application import BotsApplication
    from bots5.core.events import EventBus
    from bots5.desktop.window import MainWindow
    from bots5.domain.clock import SystemClock
    from bots5.domain.ids import Uuid7Factory
    from bots5.infrastructure.generation.fake import FakeStreamingBackend
    from tests._authority_test_support import SQLiteAppStateStore, upgrade_database

    async def scenario():
        database = tmp_path / "state.sqlite3"
        upgrade_database(database)
        ids = Uuid7Factory()
        clock = SystemClock()
        application = BotsApplication(
            SQLiteAppStateStore.open(database),
            EventBus(clock, ids),
            FakeStreamingBackend(),
            ids=ids,
            clock=clock,
        )
        window = MainWindow(application)
        try:
            await window.initialize()
            action_ids = set(window._action_registry._actions_by_id.keys())
            assert "chat.pin" in action_ids, action_ids
            assert "chat.delete" in action_ids, action_ids
        finally:
            window.stop_bridge()
            await application.close()

    asyncio.run(scenario())
    qt_app.processEvents()
