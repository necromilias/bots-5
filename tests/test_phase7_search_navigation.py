"""Deterministic Phase 7 search, navigation, and attachment oracles.

These tests exercise the real ``DataRootAuthority`` and rooted SQLite VFS.
They deliberately use private connections only to inject lost-receipt and
derived-corruption conditions that no public API is meant to create.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError as SqlAlchemyOperationalError

from bots5.core.application import BotsApplication
from bots5.core.errors import (
    SearchCursorStale,
    SearchIndexInvalid,
    SearchInvalidQuery,
    SearchResultGone,
    SearchStaleIndex,
    StateError,
)
from bots5.core.events import EventBus
from bots5.core.provider_configuration import ProviderConfiguration
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
from bots5.domain.search import (
    SearchBranchState,
    SearchDocumentKind,
    SearchFilters,
    SearchIndexCondition,
)
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.persistence.search import (
    bind_cursor_fingerprint,
    compile_literal_query,
    encode_cursor,
)
from bots5.infrastructure.persistence.transition_guard import (
    arm_phase7_source_mutation,
    clear_phase7_source_mutation,
    require_phase7_consumed,
)


BASE_TIME = datetime(2026, 9, 11, 0, 0, tzinfo=UTC)


def _open_store(root: Path):
    authority = DataRootAuthority(root.absolute()).acquire()
    try:
        return authority, authority.open_store()
    except BaseException:
        authority.close()
        raise


@contextmanager
def _store(root: Path):
    authority, store = _open_store(root)
    try:
        yield authority, store
    finally:
        if authority.state is not AuthorityState.CLOSED:
            authority.close()


def _attempt(
    chat_id: str,
    user_id: str,
    assistant_id: str,
    suffix: str,
    when: datetime,
) -> GenerationAttempt:
    return GenerationAttempt(
        id=f"attempt-{suffix}",
        chat_id=chat_id,
        user_message_id=user_id,
        assistant_message_id=assistant_id,
        backend_id="fake",
        model="fake-v0.1",
        state=AttemptState.RUNNING,
        request_snapshot=json.dumps(
            {
                "attempt_id": f"attempt-{suffix}",
                "chat_id": chat_id,
                "user_message_id": user_id,
                "backend_id": "fake",
                "model": "fake-v0.1",
                "prompt": "ignored by the legacy closed validator",
            },
            sort_keys=True,
        ),
        started_at=when,
    )


def _turn(
    store,
    chat: Chat,
    *,
    suffix: str,
    user_text: str,
    assistant_text: str,
    when: datetime,
    parent_id: str | None = None,
    user_lineage_id: str | None = None,
    user_revision: int = 1,
    user_supersedes_id: str | None = None,
) -> tuple[Chat, Message, Message]:
    user_id = f"user-{suffix}"
    assistant_id = f"assistant-{suffix}"
    sequence = store.next_message_sequence(chat.id)
    user = Message(
        user_id,
        chat.id,
        MessageRole.USER,
        MessageState.SENT,
        user_text,
        sequence,
        when,
        parent_id=parent_id,
        lineage_id=user_lineage_id or user_id,
        revision=user_revision,
        supersedes_id=user_supersedes_id,
    )
    streaming = Message(
        assistant_id,
        chat.id,
        MessageRole.ASSISTANT,
        MessageState.STREAMING,
        "",
        sequence + 1,
        when,
        parent_id=user.id,
    )
    attempt = _attempt(chat.id, user.id, streaming.id, suffix, when)
    updated = replace(
        chat,
        updated_at=when,
        head_message_id=streaming.id,
        revision=chat.revision + 1,
    )
    store.persist_generation_start(
        updated,
        user,
        streaming,
        attempt,
        expected_chat_revision=chat.revision,
    )
    assistant = replace(
        streaming,
        state=MessageState.COMPLETE,
        content=assistant_text,
    )
    store.finalize_generation(
        assistant,
        replace(attempt, state=AttemptState.COMPLETE, ended_at=when),
    )
    return updated, user, assistant


def _regenerate(
    store,
    chat: Chat,
    *,
    suffix: str,
    user: Message,
    superseded: Message,
    answer: str,
    when: datetime,
) -> tuple[Chat, Message]:
    assistant = Message(
        f"assistant-{suffix}",
        chat.id,
        MessageRole.ASSISTANT,
        MessageState.STREAMING,
        "",
        store.next_message_sequence(chat.id),
        when,
        parent_id=user.id,
        lineage_id=superseded.lineage_id,
        revision=superseded.revision + 1,
        supersedes_id=superseded.id,
    )
    attempt = _attempt(chat.id, user.id, assistant.id, suffix, when)
    updated = replace(
        chat,
        updated_at=when,
        head_message_id=assistant.id,
        revision=chat.revision + 1,
    )
    store.persist_regeneration_start(
        updated,
        assistant,
        attempt,
        expected_chat_revision=chat.revision,
    )
    terminal = replace(
        assistant,
        state=MessageState.COMPLETE,
        content=answer,
    )
    store.finalize_generation(
        terminal,
        replace(attempt, state=AttemptState.COMPLETE, ended_at=when),
    )
    return updated, terminal


def _message_keys(page) -> tuple[str, ...]:
    return tuple(
        result.document_key
        for result in page.results
        if result.document_kind is SearchDocumentKind.MESSAGE
    )


def test_global_in_chat_unicode_literal_validation_and_visibility_filters(tmp_path: Path):
    with _store(tmp_path / "root") as (_authority, store):
        chat_a = Chat("chat-a", "Alpha archive-free", BASE_TIME, BASE_TIME)
        chat_b = Chat("chat-b", "Beta archive-free", BASE_TIME, BASE_TIME)
        store.create_chat(chat_a)
        store.create_chat(chat_b)
        chat_a, user_a, _ = _turn(
            store,
            chat_a,
            suffix="a",
            user_text='café 東京 literal "quoted" operator OR wildcard* NEAR(alpha beta)',
            assistant_text="globalsearch sharedterm assistant-only",
            when=BASE_TIME + timedelta(seconds=1),
        )
        _turn(
            store,
            chat_b,
            suffix="b",
            user_text="globalsearch sharedterm operator-only",
            assistant_text="another answer",
            when=BASE_TIME + timedelta(seconds=2),
        )

        global_page = store.search(
            "globalsearch",
            filters=SearchFilters(document_kinds=(SearchDocumentKind.MESSAGE,)),
        )
        assert set(_message_keys(global_page)) == {
            "message:assistant-a",
            "message:user-b",
        }
        scoped_page = store.search(
            "globalsearch",
            filters=SearchFilters(
                chat_id=chat_a.id,
                document_kinds=(SearchDocumentKind.MESSAGE,),
            ),
        )
        assert _message_keys(scoped_page) == ("message:assistant-a",)

        unicode_page = store.search(
            "CAFÉ 東京",
            filters=SearchFilters(document_kinds=(SearchDocumentKind.MESSAGE,)),
        )
        assert _message_keys(unicode_page) == (f"message:{user_a.id}",)

        for literal in ('"quoted"', "wildcard*", "NEAR(alpha beta)"):
            page = store.search(
                literal,
                filters=SearchFilters(document_kinds=(SearchDocumentKind.MESSAGE,)),
            )
            assert _message_keys(page) == (f"message:{user_a.id}",)
        # OR must remain searchable data rather than widening the query.
        assert _message_keys(
            store.search(
                "operator OR wildcard",
                filters=SearchFilters(document_kinds=(SearchDocumentKind.MESSAGE,)),
            )
        ) == (f"message:{user_a.id}",)
        assert _message_keys(
            store.search(
                "operator OR wildcard*",
                filters=SearchFilters(document_kinds=(SearchDocumentKind.MESSAGE,)),
            )
        ) == (f"message:{user_a.id}",)

        expression, _ = compile_literal_query(
            '"quoted" OR wildcard* NEAR(alpha beta)', SearchFilters()
        )
        assert expression == '"""quoted""" "OR" "wildcard*" "NEAR(alpha" "beta)"'

        for query in ("", " \t\n", "contains\x00nul", "bad\x01control"):
            with pytest.raises(SearchInvalidQuery):
                store.search(query)
        with pytest.raises(SearchInvalidQuery):
            store.search(123)  # type: ignore[arg-type]
        with pytest.raises(SearchInvalidQuery):
            store.search("x" * 513)
        with pytest.raises(SearchInvalidQuery):
            store.search("\ud800")
        with pytest.raises(SearchInvalidQuery):
            store.search("café", filters=SearchFilters(chat_id="\ud800"))
        for field in (
            "backend_ids",
            "provider_ids",
            "models",
            "connection_ids",
            "model_entry_ids",
        ):
            with pytest.raises(SearchInvalidQuery):
                store.search(
                    "café",
                    filters=replace(SearchFilters(), **{field: ("\ud800",)}),
                )
        for invalid_limit in (0, 101, True):
            with pytest.raises(SearchInvalidQuery):
                store.search("café", limit=invalid_limit)  # type: ignore[arg-type]


def test_relevance_recency_tie_break_pagination_and_cursor_snapshot(tmp_path: Path):
    with _store(tmp_path / "root") as (_authority, store):
        corpus = (
            Chat("chat-c", "tie needle", BASE_TIME, BASE_TIME),
            Chat("chat-b", "tie needle", BASE_TIME, BASE_TIME + timedelta(seconds=2)),
            Chat("chat-a", "tie needle", BASE_TIME, BASE_TIME + timedelta(seconds=2)),
        )
        for chat in corpus:
            store.create_chat(chat)
        filters = SearchFilters(document_kinds=(SearchDocumentKind.CHAT,))
        first = store.search("tie needle", filters=filters, limit=2)
        assert [item.document_id for item in first.results] == ["chat-a", "chat-b"]
        assert first.next_cursor is not None
        second = store.search(
            "tie needle", filters=filters, limit=2, cursor=first.next_cursor
        )
        assert [item.document_id for item in second.results] == ["chat-c"]
        assert second.next_cursor is None

        with pytest.raises(SearchCursorStale):
            store.search(
                "tie needle",
                filters=replace(filters, include_archived=True),
                limit=2,
                cursor=first.next_cursor,
            )
        store.create_chat(
            Chat(
                "chat-new",
                "tie needle",
                BASE_TIME,
                BASE_TIME + timedelta(seconds=3),
            )
        )
        with pytest.raises(SearchCursorStale):
            store.search(
                "tie needle", filters=filters, limit=2, cursor=first.next_cursor
            )
        with pytest.raises(SearchCursorStale):
            store.search("tie needle", filters=filters, cursor="not-base64!")
        current = store.search("tie needle", filters=filters, limit=2)
        assert current.next_cursor is not None
        with pytest.raises(SearchCursorStale):
            store.search(
                "tie needle",
                filters=filters,
                limit=2,
                cursor=current.next_cursor + "!",
            )
        _, fingerprint = compile_literal_query("tie needle", filters)
        fingerprint = bind_cursor_fingerprint(
            fingerprint, store._search_cursor_epoch
        )
        oversized_offset = encode_cursor(
            fingerprint,
            current.status.checkpoint_revision,
            current.status.generation,
            1 << 63,
        )
        with pytest.raises(SearchCursorStale):
            store.search(
                "tie needle",
                filters=filters,
                limit=2,
                cursor=oversized_offset,
            )


def test_archive_default_include_unarchive_and_exact_navigation(tmp_path: Path):
    with _store(tmp_path / "root") as (_authority, store):
        chat = Chat("chat", "archivable", BASE_TIME, BASE_TIME)
        store.create_chat(chat)
        chat, user, _ = _turn(
            store,
            chat,
            suffix="archive",
            user_text="archive-search-needle",
            assistant_text="archive answer",
            when=BASE_TIME + timedelta(seconds=1),
        )
        before = store.search_status()
        archived = store.archive_chat(chat.id, BASE_TIME + timedelta(seconds=2))
        after = store.search_status()
        assert archived.archived_at is not None
        assert after.source_revision == before.source_revision + 1
        assert after.checkpoint_revision == after.source_revision
        filters = SearchFilters(document_kinds=(SearchDocumentKind.MESSAGE,))
        assert store.search("archive-search-needle", filters=filters).results == ()
        included = store.search(
            "archive-search-needle",
            filters=replace(filters, include_archived=True),
        )
        assert [item.document_id for item in included.results] == [user.id]
        result = included.results[0]
        assert result.include_archived is True
        assert result.locations[0].archived_at == archived.archived_at
        navigation = store.resolve_search_result(result)
        assert navigation.chat.id == chat.id
        assert navigation.focus_message_id == user.id
        assert navigation.archived_at == archived.archived_at

        unarchived = store.archive_chat(
            chat.id, None, expected_revision=archived.revision
        )
        assert unarchived.archived_at is None
        assert [item.document_id for item in store.search(
            "archive-search-needle", filters=filters
        ).results] == [user.id]


def test_surviving_revisions_regeneration_active_filter_and_historical_navigation(
    tmp_path: Path,
):
    with _store(tmp_path / "root") as (_authority, store):
        chat = Chat("chat", "lineage", BASE_TIME, BASE_TIME)
        store.create_chat(chat)
        chat, original_user, original_assistant = _turn(
            store,
            chat,
            suffix="original",
            user_text="original-user-needle",
            assistant_text="original-assistant-needle",
            when=BASE_TIME + timedelta(seconds=1),
        )
        chat, edited_user, edited_assistant = _turn(
            store,
            chat,
            suffix="edited",
            user_text="edited-user-shared-ancestor",
            assistant_text="first-regeneration-sibling",
            when=BASE_TIME + timedelta(seconds=2),
            parent_id=original_user.parent_id,
            user_lineage_id=original_user.lineage_id,
            user_revision=2,
            user_supersedes_id=original_user.id,
        )
        chat, regenerated_assistant = _regenerate(
            store,
            chat,
            suffix="regenerated",
            user=edited_user,
            superseded=edited_assistant,
            answer="second-regeneration-sibling",
            when=BASE_TIME + timedelta(seconds=3),
        )
        authoritative_head = store.get_chat(chat.id).head_message_id
        assert authoritative_head == regenerated_assistant.id

        kinds = SearchFilters(document_kinds=(SearchDocumentKind.MESSAGE,))
        cases = {
            "original-user-needle": (original_user, SearchBranchState.HISTORICAL),
            "original-assistant-needle": (
                original_assistant,
                SearchBranchState.HISTORICAL,
            ),
            "edited-user-shared-ancestor": (edited_user, SearchBranchState.ACTIVE),
            "first-regeneration-sibling": (
                edited_assistant,
                SearchBranchState.HISTORICAL,
            ),
            "second-regeneration-sibling": (
                regenerated_assistant,
                SearchBranchState.ACTIVE,
            ),
        }
        for query, (message, branch_state) in cases.items():
            page = store.search(query, filters=kinds)
            assert [item.document_id for item in page.results] == [message.id]
            assert page.results[0].locations[0].branch_state is branch_state

        original_result = store.search(
            "original-user-needle", filters=kinds
        ).results[0]
        assert original_result.lineage_id == edited_user.lineage_id
        assert original_result.revision == 1
        assert original_result.supersedes_message_id is None
        edited_result = store.search(
            "edited-user-shared-ancestor", filters=kinds
        ).results[0]
        assert edited_result.revision == 2
        assert edited_result.supersedes_message_id == original_user.id
        sibling_result = store.search(
            "first-regeneration-sibling", filters=kinds
        ).results[0]
        assert sibling_result.lineage_id == regenerated_assistant.lineage_id
        assert sibling_result.supersedes_message_id is None
        regenerated_result = store.search(
            "second-regeneration-sibling", filters=kinds
        ).results[0]
        assert regenerated_result.supersedes_message_id == edited_assistant.id

        active_only = replace(kinds, active_branch_only=True)
        assert store.search("original-user-needle", filters=active_only).results == ()
        assert store.search("first-regeneration-sibling", filters=active_only).results == ()
        assert [item.document_id for item in store.search(
            "edited-user-shared-ancestor", filters=active_only
        ).results] == [edited_user.id]

        historical = store.resolve_search_result(
            store.search("original-assistant-needle", filters=kinds).results[0]
        )
        assert historical.branch_state is SearchBranchState.HISTORICAL
        assert historical.historical_leaf_message_id == original_assistant.id
        assert historical.focus_message_id == original_assistant.id
        assert [item.id for item in historical.messages] == [
            original_user.id,
            original_assistant.id,
        ]
        sibling = store.resolve_search_result(sibling_result)
        assert [item.id for item in sibling.messages] == [
            edited_user.id,
            edited_assistant.id,
        ]
        assert store.get_chat(chat.id).head_message_id == authoritative_head


def test_result_that_disappears_is_gone_without_substitution(tmp_path: Path):
    with _store(tmp_path / "root") as (_authority, store):
        chat = Chat("gone-chat", "gone-result-needle", BASE_TIME, BASE_TIME)
        store.create_chat(chat)
        result = store.search(
            "gone-result-needle",
            filters=SearchFilters(document_kinds=(SearchDocumentKind.CHAT,)),
        ).results[0]
        with store._search_source_transaction(
            "test authoritative chat deletion", (f"chat:{chat.id}",)
        ) as connection:
            connection.execute(text("DELETE FROM chats WHERE id=:id"), {"id": chat.id})
        with pytest.raises(SearchResultGone, match="gone"):
            store.resolve_search_result(result)


def test_result_that_leaves_archive_or_active_visibility_is_gone(tmp_path: Path):
    with _store(tmp_path / "root") as (_authority, store):
        chat = Chat("visibility-chat", "visibility", BASE_TIME, BASE_TIME)
        store.create_chat(chat)
        chat, user, assistant = _turn(
            store,
            chat,
            suffix="visibility",
            user_text="visibility-user-needle",
            assistant_text="visibility-active-needle",
            when=BASE_TIME + timedelta(seconds=1),
        )
        message_filter = SearchFilters(
            document_kinds=(SearchDocumentKind.MESSAGE,)
        )
        default_result = store.search(
            "visibility-user-needle", filters=message_filter
        ).results[0]
        active_result = store.search(
            "visibility-active-needle",
            filters=replace(message_filter, active_branch_only=True),
        ).results[0]
        assert active_result.active_branch_only is True

        archived = store.archive_chat(chat.id, BASE_TIME + timedelta(seconds=2))
        with pytest.raises(SearchResultGone, match="no longer visible"):
            store.resolve_search_result(default_result)
        chat = store.archive_chat(chat.id, None, expected_revision=archived.revision)

        chat, _replacement = _regenerate(
            store,
            chat,
            suffix="visibility-replacement",
            user=user,
            superseded=assistant,
            answer="replacement answer",
            when=BASE_TIME + timedelta(seconds=3),
        )
        assert chat.head_message_id != assistant.id
        with pytest.raises(SearchResultGone, match="no longer visible"):
            store.resolve_search_result(active_result)


def test_attachment_identity_text_filename_locations_and_ineligible_exclusion(
    tmp_path: Path,
):
    async def scenario():
        authority, store = _open_store(tmp_path / "root")
        ids = Uuid7Factory()
        clock = SystemClock()
        application = BotsApplication(
            store,
            EventBus(clock, ids, queue_size=64),
            FakeStreamingBackend(),
            ids=ids,
            clock=clock,
            configuration=ProviderConfiguration(store, ids, clock),
        )
        try:
            text_one = tmp_path / "alpha-search-filename.txt"
            text_two = tmp_path / "duplicate-search-filename.txt"
            hidden = tmp_path / "hidden-search-filename.txt"
            invalid = tmp_path / "invalid-search-filename.bin"
            nul = tmp_path / "nul-search-filename.txt"
            text_one.write_bytes(b"verified attachment-body-needle")
            text_two.write_bytes(text_one.read_bytes())
            hidden.write_bytes(b"unreferenced-body-needle")
            invalid.write_bytes(b"invalid-body-needle-\xff")
            nul.write_bytes(b"nul-body-needle\x00tail")

            first = await application.attach_file(text_one)
            duplicate = await application.attach_file(text_two)
            unreferenced = await application.attach_file(hidden)
            invalid_attachment = await application.attach_file(invalid)
            nul_attachment = await application.attach_file(nul)
            assert first.id != duplicate.id
            assert first.blob_digest == duplicate.blob_digest
            assert invalid_attachment.ineligibility_reason == "invalid_utf8"
            assert nul_attachment.ineligibility_reason == "contains_nul"

            chat = await application.create_chat("attachment navigation")
            await application.stage_attachment(chat.id, first.id)
            await application.stage_attachment(chat.id, duplicate.id)
            attempt = await application.send_message(chat.id, "first attachment turn")
            await application._generation_tasks[attempt.id]

            content_results = store.search(
                "attachment-body-needle",
                filters=SearchFilters(
                    document_kinds=(SearchDocumentKind.ATTACHMENT,)
                ),
            ).results
            assert {result.document_id for result in content_results} == {
                first.id,
                duplicate.id,
            }
            assert all(len(result.locations) == 1 for result in content_results)
            filename = store.search(
                "alpha-search-filename",
                filters=SearchFilters(
                    document_kinds=(SearchDocumentKind.ATTACHMENT,)
                ),
            )
            assert [result.document_id for result in filename.results] == [first.id]

            await application.stage_attachment(chat.id, first.id)
            second_attempt = await application.send_message(
                chat.id, "second attachment turn"
            )
            await application._generation_tasks[second_attempt.id]
            first_result = next(
                result
                for result in store.search(
                    "attachment-body-needle",
                    filters=SearchFilters(
                        document_kinds=(SearchDocumentKind.ATTACHMENT,)
                    ),
                ).results
                if result.document_id == first.id
            )
            assert len(first_result.locations) == 2
            assert len({location.message_id for location in first_result.locations}) == 2
            for index, location in enumerate(first_result.locations):
                navigation = store.resolve_search_result(first_result, location_index=index)
                assert navigation.chat.id == location.chat_id
                assert navigation.focus_message_id == location.message_id

            # Rewind the first chat so its two existing references are historical,
            # then attach the same identity once on each active chat branch.
            edit_attempt = await application.edit_message(
                chat.id,
                attempt.user_message_id,
                "edited attachment branch",
            )
            await application._generation_tasks[edit_attempt.id]
            await application.stage_attachment(chat.id, first.id)
            active_attempt = await application.send_message(
                chat.id, "active attachment reference"
            )
            await application._generation_tasks[active_attempt.id]

            other_chat = await application.create_chat("other attachment chat")
            await application.stage_attachment(other_chat.id, first.id)
            other_attempt = await application.send_message(
                other_chat.id, "other attachment reference"
            )
            await application._generation_tasks[other_attempt.id]

            scoped_result = next(
                result
                for result in store.search(
                    "attachment-body-needle",
                    filters=SearchFilters(
                        chat_id=chat.id,
                        document_kinds=(SearchDocumentKind.ATTACHMENT,),
                    ),
                ).results
                if result.document_id == first.id
            )
            assert {location.chat_id for location in scoped_result.locations} == {chat.id}
            assert {
                location.branch_state for location in scoped_result.locations
            } == {SearchBranchState.ACTIVE, SearchBranchState.HISTORICAL}

            active_scoped_result = store.search(
                "attachment-body-needle",
                filters=SearchFilters(
                    chat_id=chat.id,
                    document_kinds=(SearchDocumentKind.ATTACHMENT,),
                    active_branch_only=True,
                ),
            ).results[0]
            assert len(active_scoped_result.locations) == 1
            assert active_scoped_result.locations[0].chat_id == chat.id
            assert (
                active_scoped_result.locations[0].branch_state
                is SearchBranchState.ACTIVE
            )

            assert store.search(
                "unreferenced-body-needle",
                filters=SearchFilters(
                    document_kinds=(SearchDocumentKind.ATTACHMENT,)
                ),
            ).results == ()
            assert store.search(
                "hidden-search-filename",
                filters=SearchFilters(
                    document_kinds=(SearchDocumentKind.ATTACHMENT,)
                ),
            ).results == ()
            assert store.search(
                "invalid-body-needle",
                filters=SearchFilters(
                    document_kinds=(SearchDocumentKind.ATTACHMENT,)
                ),
            ).results == ()
            assert store.search(
                "nul-body-needle",
                filters=SearchFilters(
                    document_kinds=(SearchDocumentKind.ATTACHMENT,)
                ),
            ).results == ()
            invalid_expression, _ = compile_literal_query(
                "invalid-body-needle", SearchFilters()
            )
            nul_expression, _ = compile_literal_query(
                "nul-body-needle", SearchFilters()
            )
            # The invisible identities still exist exactly once in the derived
            # document map; their ineligible payload bytes are absent from FTS,
            # and unreferenced exclusion occurs at user-visible resolution.
            with store.command_admission(), store._engine.connect() as connection:
                mapped = connection.exec_driver_sql(
                    "SELECT document_id FROM search_document_keys "
                    "WHERE document_kind='attachment'"
                ).scalars().all()
                assert connection.exec_driver_sql(
                    "SELECT document_key FROM search_fts WHERE search_fts MATCH ?",
                    (invalid_expression,),
                ).fetchall() == []
                assert connection.exec_driver_sql(
                    "SELECT document_key FROM search_fts WHERE search_fts MATCH ?",
                    (nul_expression,),
                ).fetchall() == []
            assert set(mapped) == {
                first.id,
                duplicate.id,
                unreferenced.id,
                invalid_attachment.id,
                nul_attachment.id,
            }
        finally:
            await application.close()
            assert authority.state is AuthorityState.CLOSED

    asyncio.run(scenario())


@pytest.mark.parametrize("cancelled", (False, True), ids=("ordinary", "cancelled"))
def test_application_rebuild_real_authority_settles_event_without_context_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
):
    """The tracked caller owns both worker and event; neither inherits its grant."""

    async def scenario() -> None:
        authority, store = _open_store(tmp_path / f"root-{cancelled}")
        ids = Uuid7Factory()
        clock = SystemClock()
        application = BotsApplication(
            store,
            EventBus(clock, ids),
            FakeStreamingBackend(),
            ids=ids,
            clock=clock,
        )
        subscription = application.subscribe()
        worker_started = threading.Event()
        release_worker = threading.Event()

        if cancelled:
            original_rebuild = type(store).rebuild_search_index

            def blocked_rebuild(self):
                worker_started.set()
                assert release_worker.wait(5), "rebuild worker barrier was not released"
                return original_rebuild(self)

            monkeypatch.setattr(type(store), "rebuild_search_index", blocked_rebuild)

        try:
            rebuild = asyncio.create_task(application.rebuild_search_index())
            if cancelled:
                submission_turn = asyncio.Event()
                asyncio.get_running_loop().call_soon(submission_turn.set)
                await submission_turn.wait()
                assert worker_started.wait(5), "rebuild worker did not reach barrier"
                for _attempt in range(2):
                    rebuild.cancel()
                    cancellation_turn = asyncio.Event()
                    asyncio.get_running_loop().call_soon(cancellation_turn.set)
                    await cancellation_turn.wait()
                    assert not rebuild.done()
                    assert application._active_commands == 1
                    assert not application._commands_idle.is_set()
                release_worker.set()
                with pytest.raises(asyncio.CancelledError):
                    await rebuild
            else:
                status = await asyncio.wait_for(rebuild, timeout=10)
                assert status.condition is SearchIndexCondition.VALID

            event = await asyncio.wait_for(subscription.__anext__(), timeout=10)
            assert event.kind == "search_index_rebuilt"
            assert event.payload == {
                "condition": "VALID",
                "source_revision": 0,
                "checkpoint_revision": 0,
                "generation": 1,
            }
            status = store.search_status()
            assert status.condition is SearchIndexCondition.VALID
            assert status.source_revision == status.checkpoint_revision == 0
            assert status.generation == 1
            assert application._active_commands == 0
            assert application._commands_idle.is_set()
            assert authority.state is AuthorityState.READY
        finally:
            release_worker.set()
            subscription.close()
            await application.close()
            assert authority.state is AuthorityState.CLOSED

    asyncio.run(scenario())


def test_lost_receipt_restart_refusal_and_deterministic_rebuild(tmp_path: Path):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    chat = Chat("chat", "before-lost-receipt", BASE_TIME, BASE_TIME)
    store.create_chat(chat)
    before = store.search_status()
    try:
        with store.command_admission(), store._authority.transition():
            with store._engine.begin() as connection:
                arm_phase7_source_mutation(connection, "test lost receipt")
                try:
                    connection.exec_driver_sql(
                        "UPDATE chats SET title=? WHERE id=?",
                        ("after-lost-receipt", chat.id),
                    )
                    committed_revision = require_phase7_consumed(connection)
                finally:
                    clear_phase7_source_mutation(connection)
            store._record_committed_search_source_revision(committed_revision)
        assert committed_revision == before.source_revision + 1
        status = store.search_status()
        assert status.condition is SearchIndexCondition.STALE
        assert status.source_revision == committed_revision
        assert status.checkpoint_revision == before.checkpoint_revision
        with pytest.raises(SearchStaleIndex):
            store.search("after-lost-receipt")
    finally:
        authority.close()

    restarted_authority, restarted = _open_store(root)
    try:
        assert restarted.search_status().condition is SearchIndexCondition.STALE
        with pytest.raises(SearchStaleIndex):
            restarted.search("after-lost-receipt")
        rebuilt = restarted.rebuild_search_index()
        assert rebuilt.condition is SearchIndexCondition.VALID
        assert rebuilt.source_revision == rebuilt.checkpoint_revision
        assert [item.document_id for item in restarted.search(
            "after-lost-receipt",
            filters=SearchFilters(document_kinds=(SearchDocumentKind.CHAT,)),
        ).results] == [chat.id]
        first_rows = _derived_rows(restarted)
        repeated = restarted.rebuild_search_index()
        assert repeated.condition is SearchIndexCondition.VALID
        assert _derived_rows(restarted) == first_rows
    finally:
        restarted_authority.close()


def _derived_rows(store) -> tuple[tuple[object, ...], ...]:
    with store.command_admission(), store._engine.connect() as connection:
        return tuple(
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT k.fts_rowid, k.document_kind, k.document_id, "
                "f.document_key, f.title, f.body, f.filename "
                "FROM search_document_keys k JOIN search_fts f ON f.rowid=k.fts_rowid "
                "ORDER BY k.fts_rowid"
            ).fetchall()
        )


@pytest.mark.parametrize(
    "corruption",
    (
        "deleted",
        "mismapped",
        "structural",
        "forged-key",
        "forged-title",
        "forged-body",
        "forged-filename",
    ),
)
def test_explicit_diagnostics_durably_invalidates_corruption_until_rebuild(
    tmp_path: Path,
    corruption: str,
):
    with _store(tmp_path / "root") as (_authority, store):
        chat = Chat("chat", "corruption-rebuild-needle", BASE_TIME, BASE_TIME)
        store.create_chat(chat)
        with store.command_admission(), store._engine.begin() as connection:
            rowid = int(
                connection.exec_driver_sql(
                    "SELECT fts_rowid FROM search_document_keys "
                    "WHERE document_kind='chat' AND document_id=?",
                    (chat.id,),
                ).scalar_one()
            )
            if corruption == "deleted":
                connection.exec_driver_sql(
                    "DELETE FROM search_fts WHERE rowid=?", (rowid,)
                )
                connection.exec_driver_sql(
                    "DELETE FROM search_document_keys WHERE fts_rowid=?", (rowid,)
                )
            elif corruption == "mismapped":
                connection.exec_driver_sql(
                    "UPDATE search_document_keys SET document_id='logical-ghost' "
                    "WHERE document_kind='chat' AND document_id=?",
                    (chat.id,),
                )
            elif corruption == "structural":
                connection.exec_driver_sql(
                    "DELETE FROM search_fts WHERE rowid=?", (rowid,)
                )
            else:
                column = {
                    "forged-key": "document_key",
                    "forged-title": "title",
                    "forged-body": "body",
                    "forged-filename": "filename",
                }[corruption]
                connection.exec_driver_sql(
                    f"UPDATE search_fts SET {column}=? WHERE rowid=?",
                    ("forged-derived-value", rowid),
                )
        diagnosed = store.diagnose_search_index()
        assert diagnosed.condition is SearchIndexCondition.INVALID
        # Once the explicit expensive boundary has mechanically observed an
        # invalid derived index, the durable singleton must stop ordinary
        # queries rather than continue advertising the prior VALID state.
        assert store.search_status().condition is SearchIndexCondition.INVALID
        with pytest.raises(SearchIndexInvalid):
            store.search("corruption-rebuild-needle")
        rebuilt = store.rebuild_search_index()
        assert rebuilt.condition is SearchIndexCondition.VALID
        assert store.diagnose_search_index().condition is SearchIndexCondition.VALID
        assert [item.document_id for item in store.search(
            "corruption-rebuild-needle",
            filters=SearchFilters(document_kinds=(SearchDocumentKind.CHAT,)),
        ).results] == [chat.id]


@pytest.mark.parametrize(
    "document_kind,column,forged_term,authoritative_term,document_id",
    (
        (
            SearchDocumentKind.CHAT,
            "title",
            "forged-chat-before-diagnostics",
            "authoritative-chat-before-diagnostics",
            "chat",
        ),
        (
            SearchDocumentKind.MESSAGE,
            "body",
            "forged-message-before-diagnostics",
            "authoritative-message-before-diagnostics",
            "user-forged",
        ),
    ),
)
def test_forged_returned_fts_payload_is_invalidated_before_diagnostics(
    tmp_path: Path,
    document_kind: SearchDocumentKind,
    column: str,
    forged_term: str,
    authoritative_term: str,
    document_id: str,
):
    with _store(tmp_path / "root") as (authority, store):
        chat = Chat("chat", "authoritative-chat-before-diagnostics", BASE_TIME, BASE_TIME)
        store.create_chat(chat)
        _turn(
            store,
            chat,
            suffix="forged",
            user_text="authoritative-message-before-diagnostics",
            assistant_text="terminal answer",
            when=BASE_TIME + timedelta(seconds=1),
        )
        with store.command_admission(), store._engine.begin() as connection:
            rowid = connection.exec_driver_sql(
                "SELECT fts_rowid FROM search_document_keys "
                "WHERE document_kind=? AND document_id=?",
                (document_kind.value, document_id),
            ).scalar_one()
            connection.exec_driver_sql(
                f"UPDATE search_fts SET {column}=? WHERE rowid=?",
                (forged_term, rowid),
            )

        assert store.search_status().condition is SearchIndexCondition.VALID
        with pytest.raises(
            SearchIndexInvalid,
            match="returned search document does not match authoritative truth",
        ):
            store.search(
                forged_term,
                filters=SearchFilters(document_kinds=(document_kind,)),
            )
        assert store.search_status().condition is SearchIndexCondition.INVALID
        assert authority.state is AuthorityState.READY
        assert store._poisoned is False
        with pytest.raises(SearchIndexInvalid):
            store.search(forged_term)

        assert store.rebuild_search_index().condition is SearchIndexCondition.VALID
        assert store.search(
            forged_term,
            filters=SearchFilters(document_kinds=(document_kind,)),
        ).results == ()
        assert [result.document_id for result in store.search(
            authoritative_term,
            filters=SearchFilters(document_kinds=(document_kind,)),
        ).results] == [document_id]


def test_forged_attachment_fts_payload_is_invalidated_before_diagnostics(
    tmp_path: Path,
):
    async def scenario() -> None:
        authority, store = _open_store(tmp_path / "root")
        ids = Uuid7Factory()
        clock = SystemClock()
        application = BotsApplication(
            store,
            EventBus(clock, ids, queue_size=32),
            FakeStreamingBackend(),
            ids=ids,
            clock=clock,
            configuration=ProviderConfiguration(store, ids, clock),
        )
        try:
            source = tmp_path / "authoritative-attachment-name.txt"
            source.write_bytes(b"authoritative attachment body")
            attachment = await application.attach_file(source)
            chat = await application.create_chat("attachment corruption chat")
            await application.stage_attachment(chat.id, attachment.id)
            attempt = await application.send_message(chat.id, "attach once")
            await application._generation_tasks[attempt.id]
            filters = SearchFilters(
                document_kinds=(SearchDocumentKind.ATTACHMENT,)
            )

            for column, forged_term in (
                ("filename", "forged-attachment-filename"),
                ("body", "forged-attachment-body"),
            ):
                with store.command_admission(), store._engine.begin() as connection:
                    rowid = connection.exec_driver_sql(
                        "SELECT fts_rowid FROM search_document_keys "
                        "WHERE document_kind='attachment' AND document_id=?",
                        (attachment.id,),
                    ).scalar_one()
                    connection.exec_driver_sql(
                        f"UPDATE search_fts SET {column}=? WHERE rowid=?",
                        (forged_term, rowid),
                    )
                with pytest.raises(
                    SearchIndexInvalid,
                    match="returned search document does not match authoritative truth",
                ):
                    store.search(forged_term, filters=filters)
                assert store.search_status().condition is SearchIndexCondition.INVALID
                assert authority.state is AuthorityState.READY
                assert store._poisoned is False
                assert store.rebuild_search_index().condition is SearchIndexCondition.VALID
                assert store.search(forged_term, filters=filters).results == ()
        finally:
            await application.close()
            assert authority.state is AuthorityState.CLOSED

    asyncio.run(scenario())


def test_valid_search_uses_bounded_returned_projection_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    with _store(tmp_path / "root") as (_authority, store):
        for index in range(4):
            store.create_chat(
                Chat(
                    f"chat-{index}",
                    "bounded returned validation needle",
                    BASE_TIME,
                    BASE_TIME + timedelta(seconds=index),
                )
            )

        projection_calls: list[str] = []
        original_projection = type(store)._projection_for_key

        def counted_projection(self, connection, document_key):
            projection_calls.append(document_key)
            return original_projection(self, connection, document_key)

        def full_index_validation_was_called(*_args, **_kwargs):
            pytest.fail("ordinary search invoked full-index diagnostics")

        monkeypatch.setattr(type(store), "_projection_for_key", counted_projection)
        monkeypatch.setattr(
            type(store),
            "_validate_search_projection_contents",
            staticmethod(full_index_validation_was_called),
        )
        page = store.search(
            "bounded returned validation needle",
            filters=SearchFilters(document_kinds=(SearchDocumentKind.CHAT,)),
            limit=1,
        )
        assert len(page.results) == 1
        assert page.next_cursor is not None
        assert len(projection_calls) == 2
        assert page.status.condition is SearchIndexCondition.VALID


def test_structural_fts_query_failure_is_typed_search_index_invalid(
    tmp_path: Path,
    monkeypatch,
):
    with _store(tmp_path / "root") as (_authority, store):
        store.create_chat(Chat("chat", "structural-query-needle", BASE_TIME, BASE_TIME))
        original = Connection.exec_driver_sql

        def fail_fts_query(self, statement, *args, **kwargs):
            if "FROM search_fts f" in str(statement):
                raise SqlAlchemyOperationalError(
                    str(statement),
                    args[0] if args else None,
                    sqlite3.DatabaseError("injected FTS structural corruption"),
                )
            return original(self, statement, *args, **kwargs)

        monkeypatch.setattr(Connection, "exec_driver_sql", fail_fts_query)
        with pytest.raises(SearchIndexInvalid) as raised:
            store.search("structural-query-needle")
        assert isinstance(raised.value.__cause__, SqlAlchemyOperationalError)


@pytest.mark.parametrize(
    "operation,failure,expected",
    (
        ("search", "rollback", "rollback is uncertain"),
        ("status", "commit", "outcome is uncertain"),
        ("diagnose", "close", "close is uncertain"),
        ("resolve", "commit", "outcome is uncertain"),
    ),
)
def test_live_search_read_snapshots_classify_unknown_settlement_and_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure: str,
    expected: str,
):
    with _store(tmp_path / "root") as (authority, store):
        store.create_chat(Chat("chat", "classified-read-needle", BASE_TIME, BASE_TIME))
        result = store.search("classified-read-needle").results[0]
        original_exec = Connection.exec_driver_sql
        original_method = getattr(Connection, failure)

        def fail_method(self, *args, **kwargs):
            del self, args, kwargs
            raise OSError(f"injected unknown search {failure}")

        def fail_search_statement(self, statement, *args, **kwargs):
            if "FROM search_fts f" in str(statement):
                raise SqlAlchemyOperationalError(
                    str(statement),
                    args[0] if args else None,
                    sqlite3.DatabaseError("injected search statement failure"),
                )
            return original_exec(self, statement, *args, **kwargs)

        monkeypatch.setattr(Connection, failure, fail_method)
        if failure == "rollback":
            monkeypatch.setattr(Connection, "exec_driver_sql", fail_search_statement)
        try:
            with pytest.raises(StateError, match=expected):
                if operation == "search":
                    store.search("classified-read-needle")
                elif operation == "status":
                    store.search_status()
                elif operation == "diagnose":
                    store.diagnose_search_index()
                else:
                    store.resolve_search_result(result)
            assert store._poisoned is True
            assert authority.state is AuthorityState.POISONED
            with pytest.raises(StateError, match="not admitting work"):
                store.assert_admitting()
        finally:
            monkeypatch.setattr(Connection, failure, original_method)
            monkeypatch.setattr(Connection, "exec_driver_sql", original_exec)


def test_logical_result_materialization_failure_durably_invalidates_snapshot(
    tmp_path: Path,
):
    with _store(tmp_path / "root") as (_authority, store):
        store.create_chat(Chat("chat", "logical-query-needle", BASE_TIME, BASE_TIME))
        with store.command_admission(), store._engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE search_document_keys SET document_id='logical-ghost' "
                "WHERE document_kind='chat' AND document_id='chat'"
            )

        with pytest.raises(SearchIndexInvalid):
            store.search("logical-query-needle")
        assert store.search_status().condition is SearchIndexCondition.INVALID
        with pytest.raises(SearchIndexInvalid):
            store.search("logical-query-needle")
        assert store.rebuild_search_index().condition is SearchIndexCondition.VALID
        assert [
            item.document_id for item in store.search("logical-query-needle").results
        ] == ["chat"]


def test_durable_rebuilding_and_invalid_status_refuse_queries(tmp_path: Path):
    with _store(tmp_path / "root") as (_authority, store):
        store.create_chat(Chat("chat", "state-needle", BASE_TIME, BASE_TIME))
        with store.command_admission(), store._engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE search_index_state SET condition='REBUILDING' WHERE singleton_id=1"
            )
        assert store.search_status().condition is SearchIndexCondition.REBUILDING
        from bots5.core.errors import SearchRebuilding

        with pytest.raises(SearchRebuilding):
            store.search("state-needle")
        with store.command_admission(), store._engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE search_index_state SET condition='INVALID' WHERE singleton_id=1"
            )
        assert store.search_status().condition is SearchIndexCondition.INVALID
        with pytest.raises(SearchIndexInvalid):
            store.search("state-needle")
        rebuilt = store.rebuild_search_index()
        assert rebuilt.condition is SearchIndexCondition.VALID
