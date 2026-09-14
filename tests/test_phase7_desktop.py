from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel
from qasync import QEventLoop

from bots5.core.inspection import InspectionField, InspectionProjection
from bots5.core.errors import (
    SearchCursorStale,
    SearchResultGone,
    SearchStaleIndex,
    SearchUnavailable,
)
from bots5.desktop.window import MainWindow
from bots5.domain.models import Chat, Message, MessageRole, MessageState
from bots5.domain.search import (
    SearchBranchState,
    SearchDocumentKind,
    SearchIndexCondition,
    SearchLocation,
    SearchNavigation,
    SearchPage,
    SearchResult,
    SearchStatus,
)


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
_ACTIVE_HEAD_OMITTED = object()


def _run_qasync(qt_application: QApplication, operation: Awaitable[None]) -> None:
    event_loop = QEventLoop(qt_application)
    asyncio.set_event_loop(event_loop)
    with event_loop:
        event_loop.run_until_complete(operation)


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 2.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for Phase 7 desktop state")
        await asyncio.sleep(0.005)


def _status(condition: SearchIndexCondition = SearchIndexCondition.VALID) -> SearchStatus:
    return SearchStatus(
        condition=condition,
        source_revision=7,
        checkpoint_revision=7 if condition is SearchIndexCondition.VALID else 6,
        generation=3,
        schema_version=1,
        tokenizer_version="unicode61-v1",
        detail=None,
    )


def _chat(*, archived: bool = False) -> Chat:
    return Chat(
        id="chat-a",
        title="Desktop search chat",
        created_at=NOW,
        updated_at=NOW,
        head_message_id="assistant-a",
        revision=2,
        archived_at=NOW if archived else None,
    )


def _messages() -> tuple[Message, Message]:
    user = Message(
        id="user-a",
        chat_id="chat-a",
        role=MessageRole.USER,
        state=MessageState.SENT,
        content="literal user text",
        sequence=1,
        created_at=NOW,
    )
    assistant = Message(
        id="assistant-a",
        chat_id="chat-a",
        role=MessageRole.ASSISTANT,
        state=MessageState.COMPLETE,
        content="literal assistant text",
        sequence=2,
        created_at=NOW,
        parent_id=user.id,
    )
    return user, assistant


def _message_result() -> SearchResult:
    return SearchResult(
        document_key="message:assistant-a",
        document_kind=SearchDocumentKind.MESSAGE,
        document_id="assistant-a",
        title="Assistant message",
        snippet="literal assistant text",
        rank=-1.5,
        authoritative_at=NOW,
        chat_id="chat-a",
        message_id="assistant-a",
        lineage_id="assistant-a",
        revision=1,
        role=MessageRole.ASSISTANT,
        state=MessageState.COMPLETE,
        locations=(
            SearchLocation(
                chat_id="chat-a",
                message_id="assistant-a",
                branch_state=SearchBranchState.HISTORICAL,
            ),
        ),
        checkpoint_revision=7,
        generation=3,
    )


class FakeSearchApplication:
    def __init__(self) -> None:
        self.chat = _chat()
        self.messages = _messages()
        self.search_calls: list[tuple[str, object, int, str | None]] = []
        self.open_calls: list[tuple[str, object]] = []
        self.archive_calls: list[str] = []
        self.unarchive_calls: list[str] = []
        self.rebuild_calls = 0
        self.search_error: Exception | None = None
        self.resolve_error: Exception | None = None
        self.rebuild_status = _status()
        self.page = SearchPage(results=(), next_cursor=None, status=_status())
        self.inspection_calls: list[tuple[str, str | None, str | None]] = []
        self.invalid_historical_leaf: str | None = None
        self.navigation = SearchNavigation(
            chat=self.chat,
            messages=self.messages,
            focus_message_id="assistant-a",
            historical_leaf_message_id="assistant-a",
            branch_state=SearchBranchState.HISTORICAL,
            archived_at=None,
        )

    async def search_status(self) -> SearchStatus:
        return _status()

    async def search(self, query, *, filters, limit=50, cursor=None) -> SearchPage:
        self.search_calls.append((query, filters, limit, cursor))
        if self.search_error is not None:
            raise self.search_error
        return self.page

    async def rebuild_search_index(self) -> SearchStatus:
        self.rebuild_calls += 1
        return self.rebuild_status

    async def resolve_search_result(self, result, *, location_index=0) -> SearchNavigation:
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.navigation

    async def list_chats(self) -> tuple[Chat, ...]:
        return (self.chat,)

    async def open_chat(self, chat_id, *, head_message_id=_ACTIVE_HEAD_OMITTED):
        self.open_calls.append((chat_id, head_message_id))
        if head_message_id == self.invalid_historical_leaf:
            raise ValueError("historical leaf no longer exists")
        return self.chat, self.messages

    async def inspect_chat(
        self, chat_id, *, message_id=None, historical_leaf_message_id=None
    ) -> InspectionProjection:
        self.inspection_calls.append(
            (chat_id, message_id, historical_leaf_message_id)
        )
        return InspectionProjection(
            chat_id=chat_id,
            selected_message_id=message_id,
            historical_leaf_message_id=historical_leaf_message_id,
            status="available",
            fields=(
                InspectionField("Historical leaf", historical_leaf_message_id or "active head"),
                InspectionField("Attempt 1 provider/model", "fake / fake-v0.1"),
            ),
        )

    async def archive_chat(self, chat_id: str) -> Chat:
        self.archive_calls.append(chat_id)
        self.chat = replace(self.chat, archived_at=NOW)
        return self.chat

    async def unarchive_chat(self, chat_id: str) -> Chat:
        self.unarchive_calls.append(chat_id)
        self.chat = replace(self.chat, archived_at=None)
        return self.chat


async def _dispose(window: MainWindow) -> None:
    await window.stop_bridge_async()
    window.hide()
    window.deleteLater()
    await asyncio.sleep(0)


def test_search_panel_constructs_global_and_in_chat_filters_with_locked_defaults():
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application = FakeSearchApplication()
        window = MainWindow(application)
        try:
            window._current_chat_id = application.chat.id
            panel = window.search_panel
            literal_query = '"quoted" OR prefix* -minus'
            panel.query_edit.setText(literal_query)

            assert panel.scope_combo.currentData() == "global"
            assert not panel.active_only.isChecked()
            assert not panel.include_archived.isChecked()
            await window._run_search(panel.request_payload(), append=False)

            query, filters, limit, cursor = application.search_calls[-1]
            assert query == literal_query
            assert filters.chat_id is None
            assert filters.document_kinds == (
                SearchDocumentKind.CHAT,
                SearchDocumentKind.MESSAGE,
                SearchDocumentKind.ATTACHMENT,
            )
            assert filters.roles == ()
            assert filters.message_states == ()
            assert not filters.active_branch_only
            assert not filters.include_archived
            assert limit == 50
            assert cursor is None

            panel.set_in_chat_scope()
            panel.chat_kind.setChecked(False)
            panel.attachment_kind.setChecked(False)
            panel.role_combo.setCurrentIndex(panel.role_combo.findData(MessageRole.USER.value))
            panel.state_combo.setCurrentIndex(panel.state_combo.findData(MessageState.SENT.value))
            panel.active_only.setChecked(True)
            panel.include_archived.setChecked(True)
            await window._run_search(panel.request_payload(), append=False)

            query, filters, _limit, _cursor = application.search_calls[-1]
            assert query == literal_query
            assert filters.chat_id == application.chat.id
            assert filters.document_kinds == (SearchDocumentKind.MESSAGE,)
            assert filters.roles == (MessageRole.USER,)
            assert filters.message_states == (MessageState.SENT,)
            assert filters.active_branch_only
            assert filters.include_archived
        finally:
            await _dispose(window)

    _run_qasync(qt_application, scenario())


def test_search_status_controls_and_explicit_rebuild_are_bounded():
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application = FakeSearchApplication()
        window = MainWindow(application)
        try:
            panel = window.search_panel
            panel.query_edit.setText("index state")

            panel.show_status(_status(SearchIndexCondition.VALID))
            assert panel.search_button.isEnabled()
            assert panel.rebuild_button.isEnabled()

            panel.show_status(_status(SearchIndexCondition.STALE))
            assert panel.status_label.property("condition") == "STALE"
            assert not panel.search_button.isEnabled()
            assert panel.rebuild_button.isEnabled()

            panel.show_status(_status(SearchIndexCondition.REBUILDING))
            assert not panel.search_button.isEnabled()
            assert not panel.rebuild_button.isEnabled()

            panel.show_status(_status(SearchIndexCondition.UNAVAILABLE))
            assert not panel.search_button.isEnabled()
            assert not panel.rebuild_button.isEnabled()

            panel.show_status(_status(SearchIndexCondition.STALE))
            await window._rebuild_search()
            assert application.rebuild_calls == 1
            assert panel.status_label.property("condition") == "VALID"
            assert panel.search_button.isEnabled()
            assert panel.rebuild_button.isEnabled()
        finally:
            await _dispose(window)

    _run_qasync(qt_application, scenario())


def test_result_rows_badge_history_archive_and_each_attachment_location():
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application = FakeSearchApplication()
        window = MainWindow(application)
        try:
            result = SearchResult(
                document_key="attachment:attachment-a",
                document_kind=SearchDocumentKind.ATTACHMENT,
                document_id="attachment-a",
                title="evidence.txt",
                snippet="verified UTF-8 evidence",
                rank=-2.0,
                authoritative_at=NOW,
                locations=(
                    SearchLocation(
                        chat_id="chat-a",
                        message_id="historical-reference",
                        branch_state=SearchBranchState.HISTORICAL,
                        archived_at=NOW,
                    ),
                    SearchLocation(
                        chat_id="chat-a",
                        message_id="active-reference",
                        branch_state=SearchBranchState.ACTIVE,
                    ),
                ),
                checkpoint_revision=7,
                generation=3,
                include_archived=True,
            )
            page = SearchPage(results=(result,), next_cursor=None, status=_status())
            window.search_panel.show_page(page)

            results = window.search_panel.results
            assert results.count() == 2
            assert "[Historical]" in results.item(0).text()
            assert "[Archived]" in results.item(0).text()
            assert "[Attachment]" in results.item(0).text()
            assert "[Historical]" not in results.item(1).text()
            assert "[Archived]" not in results.item(1).text()
            assert results.item(0).data(Qt.ItemDataRole.UserRole) == (result, 0)
            assert results.item(1).data(Qt.ItemDataRole.UserRole) == (result, 1)
        finally:
            await _dispose(window)

    _run_qasync(qt_application, scenario())


def test_exact_focus_historical_projection_guards_and_return_to_active():
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application = FakeSearchApplication()
        window = MainWindow(application)
        try:
            window.show()
            await asyncio.sleep(0)
            window._current_chat_id = application.chat.id
            window._chat_ids = [application.chat.id]
            window.rail.set_chats((application.chat,), application.chat.id)
            user, assistant = application.messages

            window._set_historical_leaf(assistant.id)
            window._render_transcript_projection(
                application.chat,
                application.messages,
                focus_message_id=assistant.id,
            )
            await asyncio.sleep(0)

            focused_row = window.transcript.message_rows[assistant.id]
            user_row = window.transcript.message_rows[user.id]
            assert window.historical_banner.isVisible()
            assert focused_row.bubble.property("searchFocus") is True
            assert focused_row.historical_badge.isVisible()
            assert not user_row.edit_button.isEnabled()
            assert not focused_row.regenerate_action.isEnabled()
            assert window.composer.isReadOnly()
            assert not window.send_button.isEnabled()
            assert not window.attachment_button.isEnabled()

            window._on_edit_message(user)
            window._on_regenerate_message(assistant)
            assert window._editing_message_id is None
            assert not window._refresh_tasks

            window._on_return_active_branch()
            await _wait_until(lambda: not window._refresh_tasks)
            assert window._historical_leaf_message_id is None
            assert not window.historical_banner.isVisible()
            assert application.open_calls[-1] == (application.chat.id, _ACTIVE_HEAD_OMITTED)
            assert not window.composer.isReadOnly()
            assert window.transcript.message_rows[user.id].edit_button.isEnabled()
            assert window.transcript.message_rows[assistant.id].regenerate_action.isEnabled()
        finally:
            await _dispose(window)

    _run_qasync(qt_application, scenario())


def test_details_consumes_core_projection_for_exact_historical_selection():
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application = FakeSearchApplication()
        window = MainWindow(application)
        try:
            _user, assistant = application.messages
            window._current_chat = application.chat
            window._current_chat_id = application.chat.id
            window._current_messages = application.messages
            window._selected_message = assistant
            window._set_historical_leaf(assistant.id)
            window.inspector_dock.show()
            await window._refresh_inspector()

            assert application.inspection_calls == [
                (application.chat.id, assistant.id, assistant.id)
            ]
            values = [label.text() for label in window.inspector.findChildren(QLabel)]
            assert "fake / fake-v0.1" in values
            assert assistant.id in values
        finally:
            await _dispose(window)

    _run_qasync(qt_application, scenario())


def test_stale_restored_inspector_leaf_reopens_active_transcript_in_same_refresh():
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application = FakeSearchApplication()
        application.invalid_historical_leaf = "deleted-historical-leaf"
        window = MainWindow(application)
        try:
            window._current_chat_id = application.chat.id
            window._set_historical_leaf(application.invalid_historical_leaf)

            await window._refresh_transcript(application.chat.id)

            assert window._historical_leaf_message_id is None
            assert window._current_chat == application.chat
            assert window._current_messages == application.messages
            assert set(window.transcript.message_rows) == {
                message.id for message in application.messages
            }
            assert application.open_calls == [
                (application.chat.id, "deleted-historical-leaf"),
                (application.chat.id, _ACTIVE_HEAD_OMITTED),
            ]
        finally:
            await _dispose(window)

    _run_qasync(qt_application, scenario())


def test_archive_and_unarchive_actions_update_header_and_rail_state():
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application = FakeSearchApplication()
        window = MainWindow(application)
        try:
            window.show()
            await asyncio.sleep(0)
            window._current_chat_id = application.chat.id
            window._chat_ids = [application.chat.id]
            window._current_chat = application.chat
            window.rail.set_chats((application.chat,), application.chat.id)
            window._sync_chat_header(application.chat)

            await window._toggle_current_chat_archive()
            assert application.archive_calls == [application.chat.id]
            assert window._current_chat is not None
            assert window._current_chat.archived_at == NOW
            assert window.archived_badge.isVisible()
            assert window.archive_button.text() == "Unarchive"
            assert "[Archived]" in window.rail.chat_list.item(0).text()

            await window._toggle_current_chat_archive()
            assert application.unarchive_calls == [application.chat.id]
            assert window._current_chat is not None
            assert window._current_chat.archived_at is None
            assert not window.archived_badge.isVisible()
            assert window.archive_button.text() == "Archive"
            assert "[Archived]" not in window.rail.chat_list.item(0).text()
        finally:
            await _dispose(window)

    _run_qasync(qt_application, scenario())


def test_typed_gone_stale_and_unavailable_failures_remain_visible():
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application = FakeSearchApplication()
        window = MainWindow(application)
        try:
            panel = window.search_panel
            panel.query_edit.setText("typed failure")
            payload = panel.request_payload()

            application.search_error = SearchStaleIndex("source revision exceeds checkpoint")
            await window._run_search(payload, append=False)
            assert panel.status_label.property("condition") == "STALE"
            assert "source revision exceeds checkpoint" in panel.status_label.text()

            application.search_error = SearchUnavailable("SQLite runtime lacks FTS5")
            await window._run_search(payload, append=False)
            assert panel.status_label.property("condition") == "UNAVAILABLE"
            assert "SQLite runtime lacks FTS5" in panel.status_label.text()

            application.search_error = None
            result = _message_result()
            panel.show_page(SearchPage(results=(result,), next_cursor=None, status=_status()))
            assert panel.results.count() == 1
            application.resolve_error = SearchResultGone("exact message was deleted")
            await window._navigate_search_result(result, 0)
            assert panel.status_label.property("condition") == "GONE"
            assert "exact message was deleted" in panel.status_label.text()
            assert panel.results.count() == 1
        finally:
            await _dispose(window)

    _run_qasync(qt_application, scenario())


def test_stale_cursor_expires_only_pagination_and_fresh_search_remains_available():
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application = FakeSearchApplication()
        window = MainWindow(application)
        try:
            panel = window.search_panel
            panel.query_edit.setText("paged result")
            result = _message_result()
            application.page = SearchPage(
                results=(result,),
                next_cursor="page-two",
                status=_status(),
            )

            await window._run_search(panel.request_payload(), append=False)
            assert panel.status_label.property("condition") == "VALID"
            assert panel.results.count() == 1
            assert not panel.load_more_button.isHidden()
            assert window._next_search_cursor == "page-two"

            application.search_error = SearchCursorStale("checkpoint changed")
            await window._load_more_search()

            assert panel.status_label.property("condition") == "VALID"
            assert "pagination expired" in panel.status_label.text()
            assert "checkpoint changed" in panel.status_label.text()
            assert panel.results.count() == 1
            assert panel.load_more_button.isHidden()
            assert window._next_search_cursor is None
            assert panel.search_button.isEnabled()

            application.search_error = None
            application.page = SearchPage(results=(), next_cursor=None, status=_status())
            await window._run_search(panel.request_payload(), append=False)
            assert application.search_calls[-1][3] is None
            assert panel.status_label.property("condition") == "VALID"
            assert panel.search_button.isEnabled()
        finally:
            await _dispose(window)

    _run_qasync(qt_application, scenario())
