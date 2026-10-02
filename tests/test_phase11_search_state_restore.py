"""Phase 11 fork R-15: faithful search-state restore.

The settled branch for R-15 is a FAITHFUL restore of all thirteen
``SearchFilters`` fields plus the search panel open/closed state and the
result cursor; a partial restore is not the accepted branch.

These tests cover, in order:

1. the ``0018_phase11_search_state`` migration (columns up, complete and
   structurally exact downgrade with data survival),
2. a full thirteen-field round trip through the real application and store
   plane, including a REAL pagination cursor string,
3. the design-mandated oracle: the restored filter set reproduces the
   IDENTICAL result set for a real corpus, and the persisted combination is
   drop-sensitive — silently losing ``include_archived``, ``after``,
   ``before``, ``active_branch_only``, ``document_kinds``, ``roles`` or
   ``message_states`` provably returns a DIFFERENT set, so the test fails,
4. poisoned filter payloads (empty chat_id, inverted range, unknown enum,
   truncated keys, non-JSON) are rejected, never silently accepted as-is,
5. the desktop wiring: a real MainWindow persists and restores the plane,
   including the panel projection and the result cursor.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Qt imports stay below the QT_QPA_PLATFORM default set above.
from PySide6.QtWidgets import QApplication  # noqa: E402
from qasync import QEventLoop  # noqa: E402

from bots5.core.application import BotsApplication  # noqa: E402
from bots5.core.events import EventBus  # noqa: E402
from bots5.domain.clock import SystemClock  # noqa: E402
from bots5.domain.ids import Uuid7Factory  # noqa: E402
from bots5.domain.models import (  # noqa: E402
    AttemptState,
    Chat,
    GenerationAttempt,
    Message,
    MessageRole,
    MessageState,
)
from bots5.domain.search import (  # noqa: E402
    SearchDocumentKind,
    SearchFilters,
)
from bots5.desktop.window import MainWindow  # noqa: E402
from bots5.infrastructure.data_root_authority import (  # noqa: E402
    AuthorityState,
    DataRootAuthority,
)
from bots5.infrastructure.generation.fake import FakeStreamingBackend  # noqa: E402
from tests._authority_test_support import (  # noqa: E402
    SQLiteAppStateStore,
    _root_for,
    downgrade_to,
    upgrade_to,
)


HEAD = "0018_phase11_search_state"
PRIOR_HEAD = "0017_phase11_integrity"

T_EARLY = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
T_IN_A = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
T_IN_B = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)
T_LATE = datetime(2026, 9, 25, 0, 0, tzinfo=UTC)
AFTER = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)
BEFORE = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Store helpers (real DataRootAuthority; mirrors the Phase 7 search oracles)
# ---------------------------------------------------------------------------


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
        lineage_id=user_id,
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
    assistant = replace(streaming, state=MessageState.COMPLETE, content=assistant_text)
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
    terminal = replace(assistant, state=MessageState.COMPLETE, content=answer)
    store.finalize_generation(
        terminal,
        replace(attempt, state=AttemptState.COMPLETE, ended_at=when),
    )
    return updated, terminal


def _result_keys(page) -> tuple[str, ...]:
    return tuple(result.document_key for result in page.results)


# ---------------------------------------------------------------------------
# 1. Migration schema
# ---------------------------------------------------------------------------


_SEARCH_COLUMNS = ("search_open", "search_query", "search_filters_json", "search_cursor")


def _table_info(database: Path, table: str) -> list[tuple]:
    with sqlite3.connect(database) as conn:
        return conn.execute(f"PRAGMA table_info({table})").fetchall()


class TestMigration0018Schema:
    def test_upgrade_adds_the_search_state_columns(self, tmp_path: Path):
        database = tmp_path / "state.sqlite3"
        upgrade_to(database, HEAD)

        columns = {row[1]: row for row in _table_info(database, "workspace_windows")}
        for name in _SEARCH_COLUMNS:
            assert name in columns, f"0018 missing column {name}"
        # The open flag is a non-null boolean defaulting to closed, exactly
        # like a legacy window row that predates the feature.
        assert columns["search_open"][3] == 1
        assert columns["search_open"][4] == "0"
        for name in ("search_query", "search_filters_json", "search_cursor"):
            assert columns[name][3] == 0, f"{name} must stay nullable"

        with sqlite3.connect(database) as conn:
            version = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
        assert version == HEAD

    def test_downgrade_restores_prior_schema_exactly_and_keeps_data(
        self, tmp_path: Path
    ):
        database = tmp_path / "state.sqlite3"
        upgrade_to(database, PRIOR_HEAD)
        with sqlite3.connect(database) as conn:
            original_ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='workspace_windows'"
            ).fetchone()[0]
            original_objects = conn.execute(
                "SELECT type, name, tbl_name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            conn.execute(
                "INSERT INTO workspace_windows(window_id, ordinal, rail_collapsed, "
                "restore_open, inspector_open, maximized, updated_at) "
                "VALUES ('win-legacy', 4, 0, 1, 0, 0, '2026-09-01T00:00:00.000Z')"
            )
            conn.commit()

        upgrade_to(database, HEAD)
        with sqlite3.connect(database) as conn:
            conn.execute(
                "INSERT INTO workspace_windows(window_id, ordinal, rail_collapsed, "
                "restore_open, inspector_open, maximized, search_open, search_query, "
                "search_filters_json, search_cursor, updated_at) "
                "VALUES ('win-data', 3, 1, 1, 0, 0, 1, 'needle', '{}', 'cur', "
                "'2026-09-01T00:00:00.000Z')"
            )
            conn.commit()

        downgrade_to(database, PRIOR_HEAD)

        with sqlite3.connect(database) as conn:
            version = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            downgraded_ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='workspace_windows'"
            ).fetchone()[0]
            downgraded_objects = conn.execute(
                "SELECT type, name, tbl_name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(workspace_windows)"
            ).fetchall()}
            rows = conn.execute(
                "SELECT window_id, ordinal, rail_collapsed, restore_open, updated_at "
                "FROM workspace_windows ORDER BY window_id"
            ).fetchall()

        assert version == PRIOR_HEAD
        # The previous schema is restored EXACTLY: the pre-0018 CREATE TABLE
        # text (alembic uses native ALTER on this table, so the original DDL
        # text survives) and the complete object inventory return verbatim.
        assert downgraded_ddl == original_ddl, (
            "workspace_windows DDL changed across the 0018 downgrade"
        )
        assert downgraded_objects == original_objects
        for name in _SEARCH_COLUMNS:
            assert name not in columns, f"{name} survived the downgrade"
        # Pre-existing rows survive with their original columns intact.
        assert rows == [
            ("win-data", 3, 1, 1, "2026-09-01T00:00:00.000Z"),
            ("win-legacy", 4, 0, 1, "2026-09-01T00:00:00.000Z"),
        ]


# ---------------------------------------------------------------------------
# 2. Thirteen-field round trip through the application/store plane
# ---------------------------------------------------------------------------


def _full_filters() -> SearchFilters:
    return SearchFilters(
        chat_id="chat-round",
        document_kinds=(SearchDocumentKind.MESSAGE, SearchDocumentKind.CHAT),
        roles=(MessageRole.USER,),
        message_states=(MessageState.SENT,),
        backend_ids=("fake",),
        provider_ids=("provider-r15",),
        models=("fake-v0.1",),
        connection_ids=("connection-r15",),
        model_entry_ids=("entry-r15",),
        active_branch_only=True,
        include_archived=True,
        after=datetime(2026, 9, 1, tzinfo=UTC),
        before=datetime(2026, 9, 28, tzinfo=UTC),
    )


def _application(database: Path) -> BotsApplication:
    ids = Uuid7Factory()
    clock = SystemClock()
    return BotsApplication(
        SQLiteAppStateStore.open(database),
        EventBus(clock, ids, queue_size=32),
        FakeStreamingBackend(),
        ids=ids,
        clock=clock,
    )


def test_all_thirteen_fields_query_open_flag_and_cursor_round_trip(tmp_path: Path):
    database = tmp_path / "state.sqlite3"
    SQLiteAppStateStore.open(database).close()  # migrate to head

    filters = _full_filters()
    captured: dict[str, object] = {}

    async def scenario() -> None:
        application = _application(database)
        try:
            # A REAL pagination cursor, not an invented string: one chat
            # document per page forces next_cursor out of the store.
            for index in range(3):
                await application.create_chat(title=f"cursored chat {index}")
            page = await application.search(
                "cursored",
                filters=SearchFilters(document_kinds=(SearchDocumentKind.CHAT,)),
                limit=1,
            )
            assert page.next_cursor is not None, (
                "precondition: a limited search must produce a cursor"
            )
            captured["cursor"] = page.next_cursor
            await application.save_workspace_window(
                window_id="window-r15",
                ordinal=0,
                geometry=None,
                selected_chat_id=None,
                rail_collapsed=False,
                restore_open=True,
                search_open=True,
                search_query="cursored",
                search_filters=filters,
                search_cursor=page.next_cursor,
            )
        finally:
            await application.close()

        application_reopened = _application(database)
        try:
            states = await application_reopened.list_workspace_windows()
            restored = next(
                state for state in states if state.window_id == "window-r15"
            )
        finally:
            await application_reopened.close()

        # The open/closed flag and the result cursor round-trip.
        assert restored.search_open is True
        assert restored.search_cursor == page.next_cursor, (
            "the persisted result cursor did not survive the reopen"
        )
        assert restored.search_query == "cursored"
        # FAITHFUL restore of all thirteen SearchFilters fields: every field
        # is asserted individually, so a single dropped field is named.
        assert restored.search_filters == filters
        assert restored.search_filters is not filters, "must decode a fresh value"
        assert restored.search_filters.chat_id == "chat-round"
        assert restored.search_filters.document_kinds == (
            SearchDocumentKind.MESSAGE,
            SearchDocumentKind.CHAT,
        )
        assert restored.search_filters.roles == (MessageRole.USER,)
        assert restored.search_filters.message_states == (MessageState.SENT,)
        assert restored.search_filters.backend_ids == ("fake",)
        assert restored.search_filters.provider_ids == ("provider-r15",)
        assert restored.search_filters.models == ("fake-v0.1",)
        assert restored.search_filters.connection_ids == ("connection-r15",)
        assert restored.search_filters.model_entry_ids == ("entry-r15",)
        assert restored.search_filters.active_branch_only is True
        assert restored.search_filters.include_archived is True
        assert restored.search_filters.after == datetime(2026, 9, 1, tzinfo=UTC)
        assert restored.search_filters.before == datetime(2026, 9, 28, tzinfo=UTC)

    asyncio.run(scenario())

    # The persisted payload itself carries every filter key in SQLite: the
    # value reached the database, not just an in-memory cache.
    with sqlite3.connect(database) as conn:
        row = conn.execute(
            "SELECT search_open, search_query, search_filters_json, search_cursor "
            "FROM workspace_windows WHERE window_id='window-r15'"
        ).fetchone()
    assert row is not None, "workspace window row missing from SQLite"
    assert row[0] == 1
    assert row[1] == "cursored"
    assert row[3] == captured["cursor"]
    payload = json.loads(row[2])
    assert payload == {
        "version": 1,
        "chat_id": "chat-round",
        "document_kinds": ["message", "chat"],
        "roles": ["user"],
        "message_states": ["sent"],
        "backend_ids": ["fake"],
        "provider_ids": ["provider-r15"],
        "models": ["fake-v0.1"],
        "connection_ids": ["connection-r15"],
        "model_entry_ids": ["entry-r15"],
        "active_branch_only": True,
        "include_archived": True,
        "after": "2026-09-01T00:00:00.000Z",
        "before": "2026-09-28T00:00:00.000Z",
    }


# ---------------------------------------------------------------------------
# 3. The design oracle: restored search reproduces the identical result set
# ---------------------------------------------------------------------------


def _build_result_set_corpus(root: Path) -> None:
    """Real searchable content across three chats with divergent visibility."""
    with _store(root) as (_authority, store):
        alpha = Chat("chat-alpha", "needlebound alpha", T_EARLY, T_EARLY)
        store.create_chat(alpha)
        _turn(
            store,
            alpha,
            suffix="a",
            user_text="needlebound words",
            assistant_text="needlebound answer",
            when=T_EARLY + timedelta(seconds=1),
        )
        gamma = Chat("chat-gamma", "needlebound gamma", T_IN_A, T_IN_A)
        store.create_chat(gamma)
        gamma, user_g, historical = _turn(
            store,
            gamma,
            suffix="g",
            user_text="needlebound words",
            assistant_text="needlebound historical words",
            when=T_IN_A,
        )
        _regenerate(
            store,
            gamma,
            suffix="g2",
            user=user_g,
            superseded=historical,
            answer="needlebound active words",
            when=T_LATE,
        )
        beta = Chat("chat-beta", "needlebound beta", T_IN_B, T_IN_B)
        store.create_chat(beta)
        beta, user_b, _ = _turn(
            store,
            beta,
            suffix="b",
            user_text="needlebound words",
            # The assistant reply deliberately does not contain the needle:
            # it sits inside the time bounds, so only its TEXT keeps it out
            # of every result set here and the visibility/bound fields stay
            # load-bearing for the user message.
            assistant_text="a reply without the needle term",
            when=T_IN_B,
        )
        store.archive_chat(beta.id, T_IN_B + timedelta(seconds=1))
        # A chat whose TITLE matches the needle but whose messages do not,
        # with its authoritative chat timestamp inside the bounds: its CHAT
        # document only becomes reachable when document_kinds is lost, which
        # is exactly what the drop-sensitivity oracle below needs.
        delta_when = T_IN_B + timedelta(seconds=2)
        delta = Chat("chat-delta", "needlebound delta", delta_when, delta_when)
        store.create_chat(delta)
        _turn(
            store,
            delta,
            suffix="d",
            user_text="unrelated question",
            assistant_text="an unrelated reply",
            when=delta_when,
        )


def test_restored_filter_set_reproduces_the_identical_result_set(tmp_path: Path):
    root = tmp_path / "root"
    _build_result_set_corpus(root)

    # NON-TRIVIAL filter set: kinds, visibility flags and BOTH time bounds.
    # Every one of the five asserted fields is load-bearing: the
    # drop-sensitivity block below proves the re-run set would DIFFER if any
    # of them were silently lost, so a partial restore cannot pass.
    filters = SearchFilters(
        chat_id=None,
        document_kinds=(SearchDocumentKind.MESSAGE,),
        roles=(),
        message_states=(),
        backend_ids=(),
        provider_ids=(),
        models=(),
        connection_ids=(),
        model_entry_ids=(),
        active_branch_only=True,
        include_archived=True,
        after=AFTER,
        before=BEFORE,
    )

    database = root / "database" / "state.sqlite3"

    async def scenario() -> None:
        application = _application(database)
        try:
            status = await application.search_status()
            assert status.is_searchable, "precondition: corpus index must be valid"
            original_page = await application.search("needlebound", filters=filters)
            expected = _result_keys(original_page)
            assert set(expected) == {
                "message:user-g",
                "message:user-b",
            }, f"precondition failed, got {expected}"
            await application.save_workspace_window(
                window_id="window-r15",
                ordinal=0,
                geometry=None,
                selected_chat_id=None,
                rail_collapsed=False,
                restore_open=True,
                search_open=True,
                search_query="needlebound",
                search_filters=filters,
                search_cursor=None,
            )
        finally:
            await application.close()

        # REOPEN: a brand new application over the same durable state.
        application_reopened = _application(database)
        try:
            states = await application_reopened.list_workspace_windows()
            restored = next(
                state for state in states if state.window_id == "window-r15"
            )
            assert restored.search_filters == filters
            rerun_page = await application_reopened.search(
                restored.search_query,
                filters=restored.search_filters,
            )
            rerun = _result_keys(rerun_page)
        finally:
            await application_reopened.close()

        # THE settled oracle: identical result set for the restored fields.
        assert rerun == expected, (
            "the restored search did not reproduce the original result set: "
            f"{rerun} != {expected}"
        )

        # Drop-sensitivity: silently losing ANY of these fields from the
        # persisted filter set would return a DIFFERENT result set, so this
        # test fails.  Each line is the exact mutation a partial restore
        # would accidentally perform.
        application_reopened2 = _application(database)
        try:
            for label, mutated in (
                ("include_archived", replace(restored.search_filters, include_archived=False)),
                ("after", replace(restored.search_filters, after=None)),
                ("before", replace(restored.search_filters, before=None)),
                (
                    "active_branch_only",
                    replace(restored.search_filters, active_branch_only=False),
                ),
                ("document_kinds", replace(restored.search_filters, document_kinds=())),
            ):
                dropped_page = await application_reopened2.search(
                    "needlebound", filters=mutated
                )
                assert _result_keys(dropped_page) != expected, (
                    f"the filter set is not drop-sensitive for {label}: losing "
                    f"{label} still reproduced the same result set, so the "
                    "identical-result-set oracle cannot detect its loss"
                )
        finally:
            await application_reopened2.close()

    asyncio.run(scenario())


def test_restored_roles_and_states_change_the_result_set(tmp_path: Path):
    root = tmp_path / "root"
    _build_result_set_corpus(root)

    database = root / "database" / "state.sqlite3"

    async def scenario() -> None:
        # Two filter sets, because roles and message_states overlap: inside
        # ONE set the other field would mask a loss (states=sent already
        # implies user messages).  Each set makes exactly one of them
        # load-bearing, so a silent loss of that field provably changes the
        # re-run result set.
        role_filters = SearchFilters(
            chat_id=None,
            document_kinds=(SearchDocumentKind.MESSAGE,),
            roles=(MessageRole.USER,),
            message_states=(),
            backend_ids=(),
            provider_ids=(),
            models=(),
            connection_ids=(),
            model_entry_ids=(),
            active_branch_only=False,
            include_archived=True,
            after=None,
            before=None,
        )
        state_filters = replace(role_filters, roles=(), message_states=(MessageState.SENT,))
        expected_by_window: dict[str, tuple[str, ...]] = {}

        application = _application(database)
        try:
            for window_id, filters in (
                ("window-roles", role_filters),
                ("window-states", state_filters),
            ):
                original_page = await application.search("needlebound", filters=filters)
                expected = _result_keys(original_page)
                assert set(expected) == {
                    "message:user-a",
                    "message:user-g",
                    "message:user-b",
                }, f"precondition failed for {window_id}, got {expected}"
                expected_by_window[window_id] = expected
                await application.save_workspace_window(
                    window_id=window_id,
                    ordinal=0,
                    geometry=None,
                    selected_chat_id=None,
                    rail_collapsed=False,
                    restore_open=True,
                    search_open=False,
                    search_query="needlebound",
                    search_filters=filters,
                    search_cursor=None,
                )
        finally:
            await application.close()

        application_reopened = _application(database)
        try:
            states = await application_reopened.list_workspace_windows()
            by_id = {state.window_id: state for state in states}
            for window_id, loss in (
                ("window-roles", "roles"),
                ("window-states", "message_states"),
            ):
                restored = by_id[window_id]
                rerun = _result_keys(
                    await application_reopened.search(
                        restored.search_query,
                        filters=restored.search_filters,
                    )
                )
                assert rerun == expected_by_window[window_id], (
                    f"the restored search for {window_id} did not reproduce "
                    "the original result set"
                )
                mutated = replace(restored.search_filters, **{loss: ()})
                dropped_page = await application_reopened.search(
                    "needlebound", filters=mutated
                )
                assert _result_keys(dropped_page) != expected_by_window[window_id], (
                    f"losing {loss} from the restored filter set did not "
                    "change the result set, so its loss is undetectable"
                )
        finally:
            await application_reopened.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4. Poisoned payloads are rejected, never silently accepted as-is
# ---------------------------------------------------------------------------


def _poison_filters_json(database: Path, window_id: str, payload: str) -> None:
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE workspace_windows SET search_filters_json=? WHERE window_id=?",
            (payload, window_id),
        )
        conn.commit()


def test_poisoned_filter_payloads_are_not_silently_accepted(tmp_path: Path):
    database = tmp_path / "state.sqlite3"
    SQLiteAppStateStore.open(database).close()

    # A valid reference window proves the rejection is row-scoped: a poisoned
    # search payload falls back to a fresh window and must not take the other
    # windows' presentation state down with it.
    good = _full_filters()

    async def scenario() -> None:
        application = _application(database)
        try:
            await application.save_workspace_window(
                window_id="window-good",
                ordinal=0,
                geometry=None,
                selected_chat_id=None,
                rail_collapsed=False,
                restore_open=True,
                search_open=True,
                search_query="fine",
                search_filters=good,
                search_cursor="cursor-ok",
            )
            await application.save_workspace_window(
                window_id="window-bad",
                ordinal=1,
                geometry=None,
                selected_chat_id=None,
                rail_collapsed=False,
                restore_open=True,
                search_open=True,
                search_query="poisoned",
                search_filters=good,
                search_cursor=None,
            )
        finally:
            await application.close()

        for label, payload in (
            (
                "empty chat id",
                json.dumps(
                    {
                        "version": 1,
                        "chat_id": "",
                        "document_kinds": [],
                        "roles": [],
                        "message_states": [],
                        "backend_ids": [],
                        "provider_ids": [],
                        "models": [],
                        "connection_ids": [],
                        "model_entry_ids": [],
                        "active_branch_only": False,
                        "include_archived": False,
                        "after": None,
                        "before": None,
                    }
                ),
            ),
            (
                "inverted time range",
                json.dumps(
                    {
                        "version": 1,
                        "chat_id": None,
                        "document_kinds": [],
                        "roles": [],
                        "message_states": [],
                        "backend_ids": [],
                        "provider_ids": [],
                        "models": [],
                        "connection_ids": [],
                        "model_entry_ids": [],
                        "active_branch_only": False,
                        "include_archived": False,
                        "after": "2026-09-28T00:00:00.000Z",
                        "before": "2026-09-01T00:00:00.000Z",
                    }
                ),
            ),
            (
                "unknown enum value",
                json.dumps(
                    {
                        "version": 1,
                        "chat_id": None,
                        "document_kinds": [],
                        "roles": ["moderator"],
                        "message_states": [],
                        "backend_ids": [],
                        "provider_ids": [],
                        "models": [],
                        "connection_ids": [],
                        "model_entry_ids": [],
                        "active_branch_only": False,
                        "include_archived": False,
                        "after": None,
                        "before": None,
                    }
                ),
            ),
            (
                "truncated payload",
                json.dumps(
                    {
                        "version": 1,
                        "chat_id": None,
                        "document_kinds": [],
                    }
                ),
            ),
            ("not json", "{definitely not json"),
            (
                "wrong version",
                json.dumps(
                    {
                        "version": 999,
                        "chat_id": None,
                        "document_kinds": [],
                        "roles": [],
                        "message_states": [],
                        "backend_ids": [],
                        "provider_ids": [],
                        "models": [],
                        "connection_ids": [],
                        "model_entry_ids": [],
                        "active_branch_only": False,
                        "include_archived": False,
                        "after": None,
                        "before": None,
                    }
                ),
            ),
            (
                "non-string bound",
                json.dumps(
                    {
                        "version": 1,
                        "chat_id": None,
                        "document_kinds": [],
                        "roles": [],
                        "message_states": [],
                        "backend_ids": [],
                        "provider_ids": [],
                        "models": [],
                        "connection_ids": [],
                        "model_entry_ids": [],
                        "active_branch_only": False,
                        "include_archived": False,
                        "after": 17,
                        "before": None,
                    }
                ),
            ),
            (
                "non-boolean flag",
                json.dumps(
                    {
                        "version": 1,
                        "chat_id": None,
                        "document_kinds": [],
                        "roles": [],
                        "message_states": [],
                        "backend_ids": [],
                        "provider_ids": [],
                        "models": [],
                        "connection_ids": [],
                        "model_entry_ids": [],
                        "active_branch_only": "yes",
                        "include_archived": False,
                        "after": None,
                        "before": None,
                    }
                ),
            ),
        ):
            _poison_filters_json(database, "window-bad", payload)

            application_reopened = _application(database)
            try:
                states = await application_reopened.list_workspace_windows()
            finally:
                await application_reopened.close()

            by_id = {state.window_id: state for state in states}
            assert "window-bad" not in by_id, (
                f"an {label} payload was silently accepted as window state"
            )
            # The healthy row is untouched: presentation corruption falls back
            # per window row instead of blocking durable chat state.
            assert "window-good" in by_id
            assert by_id["window-good"].search_filters == good
            # The store never rewrites the poisoned payload: it is refused,
            # not repaired in place behind the caller's back.
            with sqlite3.connect(database) as conn:
                stored = conn.execute(
                    "SELECT search_filters_json FROM workspace_windows "
                    "WHERE window_id='window-bad'"
                ).fetchone()[0]
            assert stored == payload

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 5. Desktop wiring: a real window persists and restores the plane
# ---------------------------------------------------------------------------


def _run_qasync(qt_application: QApplication, operation) -> None:
    qt_application.setQuitOnLastWindowClosed(False)
    event_loop = QEventLoop(qt_application)
    asyncio.set_event_loop(event_loop)
    with event_loop:
        event_loop.run_until_complete(operation)


async def _flush() -> None:
    for _ in range(8):
        await asyncio.sleep(0.01)


async def _dispose_window(window: MainWindow) -> None:
    try:
        if window._window_id is not None:
            await window._finish_close()
        else:
            window.stop_bridge()
    finally:
        window.deleteLater()
        for _ in range(6):
            await asyncio.sleep(0.01)


def test_search_state_round_trips_through_window_sqlite_and_rebuilt_window(
    tmp_path: Path,
):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        database = tmp_path / "state.sqlite3"
        _build_result_set_corpus(_root_for(database))
        application = _application(database)
        window = MainWindow(application)
        try:
            await window.initialize()
            window.show()
            await _flush()
            # The user opens the search panel and runs a genuinely filtered
            # search through the real widgets.
            window.search_dock.show()
            window.search_panel.query_edit.setText("needlebound")
            window.search_panel.message_kind.setChecked(True)
            window.search_panel.chat_kind.setChecked(False)
            window.search_panel.attachment_kind.setChecked(False)
            window.search_panel.active_only.setChecked(True)
            window.search_panel.include_archived.setChecked(True)
            payload = window.search_panel.request_payload()
            assert payload["query"] == "needlebound"
            await window._run_search(payload, append=False)
            await _flush()
            expected_filters = SearchFilters(
                chat_id=None,
                document_kinds=(SearchDocumentKind.MESSAGE,),
                roles=(),
                message_states=(),
                backend_ids=(),
                provider_ids=(),
                models=(),
                connection_ids=(),
                model_entry_ids=(),
                active_branch_only=True,
                include_archived=True,
                after=None,
                before=None,
            )
            assert window._last_search_filters == expected_filters, (
                "precondition: the panel search did not build the expected filters"
            )
            assert window._last_search_query == "needlebound"
            await window._save_workspace()
            await _flush()
            window_id = window._window_id

            # FEATURE-POSITIVE persistence: the search plane is in SQLite.
            with sqlite3.connect(database) as conn:
                row = conn.execute(
                    "SELECT search_open, search_query, search_filters_json, "
                    "search_cursor FROM workspace_windows WHERE window_id=?",
                    (window_id,),
                ).fetchone()
            assert row is not None, "workspace window row missing from SQLite"
            assert row[0] == 1, "open search panel was not persisted"
            assert row[1] == "needlebound"
            stored_payload = json.loads(row[2])
            for key in (
                "chat_id",
                "document_kinds",
                "roles",
                "message_states",
                "backend_ids",
                "provider_ids",
                "models",
                "connection_ids",
                "model_entry_ids",
                "active_branch_only",
                "include_archived",
                "after",
                "before",
            ):
                assert key in stored_payload, f"filter key {key} missing in SQLite"

            states = await application.list_workspace_windows()
            state = next(item for item in states if item.window_id == window_id)
            assert state.search_open is True
            assert state.search_filters == expected_filters

            # A persisted result cursor must reach the rebuilt window too.
            # (The panel always searches with the stock limit, so the real
            # cursor string round trip is pinned by the application-level
            # test; here the restore path is exercised with a cursor value.)
            state = replace(state, search_cursor="restored-cursor")
        finally:
            await _dispose_window(window)

        rebuilt = MainWindow(application, window_state=state)
        try:
            await rebuilt.initialize()
            rebuilt.show()
            await asyncio.sleep(0.01)
            await _flush()
            # FEATURE-POSITIVE restore: without the wiring the dock stays
            # hidden, the filters stay None and the cursor stays None.
            assert rebuilt.search_dock.isVisible(), (
                "rebuilt window did not reopen the search panel"
            )
            assert rebuilt._last_search_filters == expected_filters, (
                "rebuilt window lost the persisted filter set"
            )
            assert rebuilt._last_search_query == "needlebound"
            assert rebuilt._next_search_cursor == "restored-cursor", (
                "rebuilt window lost the persisted result cursor"
            )
            # The panel projection reflects the restored fields wherever the
            # UI can represent them.
            assert rebuilt.search_panel.query_edit.text() == "needlebound"
            assert rebuilt.search_panel.active_only.isChecked() is True
            assert rebuilt.search_panel.include_archived.isChecked() is True
            assert rebuilt.search_panel.message_kind.isChecked() is True
            assert rebuilt.search_panel.chat_kind.isChecked() is False
            assert rebuilt.search_panel.attachment_kind.isChecked() is False
        finally:
            await _dispose_window(rebuilt)
            await application.close()

    _run_qasync(qt_application, scenario())
