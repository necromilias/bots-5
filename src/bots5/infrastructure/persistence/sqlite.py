from __future__ import annotations

import json
import re

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import Engine, create_engine, delete, event, func, insert, select, text, update

from bots5.core.errors import RevisionConflict, StateError
from bots5.domain.clock import parse_utc, utc_iso
from bots5.domain.models import (
    AttemptState,
    Chat,
    GenerationAttempt,
    Message,
    MessageRole,
    MessageState,
    WorkspaceWindowState,
)

from .schema import chats, generation_attempts, messages, workspace_windows
from .phase3_validation import (
    PHASE3_BACKEND_ID,
    is_phase3_record,
    validate_remote_outcome_transition,
    validate_outcome_fields,
    validate_request_snapshot,
)
from .transition_guard import (
    arm_transition,
    clear_transition,
    install_transition_guard,
)


_MESSAGE_STATES = {state.value for state in MessageState}
_MESSAGE_TERMINAL_STATES = {
    MessageState.COMPLETE,
    MessageState.INCOMPLETE,
    MessageState.TRUNCATED,
    MessageState.FAILED,
    MessageState.ABORTED,
}
_ATTEMPT_TERMINAL_STATES = {
    AttemptState.COMPLETE,
    AttemptState.INCOMPLETE,
    AttemptState.FAILED,
    AttemptState.ABORTED,
}
_ATTEMPT_TO_MESSAGE_STATE = {
    AttemptState.COMPLETE: MessageState.COMPLETE,
    AttemptState.INCOMPLETE: MessageState.INCOMPLETE,
    AttemptState.FAILED: MessageState.FAILED,
    AttemptState.ABORTED: MessageState.ABORTED,
}
_PHASE3_COLUMN_TYPES = {
    "provider_id": "VARCHAR(64)",
    "returned_model": "TEXT",
    "request_id": "TEXT",
    "finish_reason": "VARCHAR(128)",
    "prompt_tokens": "INTEGER",
    "completion_tokens": "INTEGER",
    "reasoning_tokens": "INTEGER",
    "total_tokens": "INTEGER",
    "known_cost_usd": "TEXT",
    "remote_outcome_unknown": "BOOLEAN",
}
_PHASE4_WORKSPACE_COLUMN_TYPES = {
    "window_id": "VARCHAR(128)",
    "ordinal": "INTEGER",
    "geometry_json": "TEXT",
    "selected_chat_id": "VARCHAR(64)",
    "rail_collapsed": "BOOLEAN",
    "restore_open": "BOOLEAN",
    "updated_at": "VARCHAR(40)",
}
_PHASE4_WORKSPACE_NOT_NULL = {
    "ordinal",
    "rail_collapsed",
    "restore_open",
    "updated_at",
}
_PHASE4_ACTIVE_INDEX_PREDICATE = re.compile(
    r"\(?\s*(?i:state|\"state\"|`state`|\[state\])\s*=\s*'running'\s*\)?",
)


def _validate_request_snapshot(
    attempt: GenerationAttempt,
    user_message_content: object | None = None,
    *,
    phase3: bool | None = None,
) -> None:
    validate_request_snapshot(
        attempt_id=attempt.id,
        chat_id=attempt.chat_id,
        user_message_id=attempt.user_message_id,
        backend_id=attempt.backend_id,
        model=attempt.model,
        provider_id=attempt.provider_id,
        request_snapshot=attempt.request_snapshot,
        user_message_content=user_message_content,
        phase3=phase3,
        error_type=StateError,
    )


def _validate_attempt_outcome(
    attempt: GenerationAttempt,
    *,
    phase3: bool | None = None,
) -> None:
    if phase3 is None:
        phase3 = attempt.provider_id is not None or attempt.backend_id == PHASE3_BACKEND_ID
    validate_outcome_fields(
        state=attempt.state.value,
        provider_id=attempt.provider_id,
        finish_reason=attempt.finish_reason,
        prompt_tokens=attempt.prompt_tokens,
        completion_tokens=attempt.completion_tokens,
        reasoning_tokens=attempt.reasoning_tokens,
        total_tokens=attempt.total_tokens,
        known_cost_usd=attempt.known_cost_usd,
        remote_outcome_unknown=attempt.remote_outcome_unknown,
        returned_model=attempt.returned_model,
        request_id=attempt.request_id,
        outcome_error_type=attempt.error_type,
        outcome_error_message=attempt.error_message,
        phase3=phase3,
        error_type=StateError,
    )


def _is_persisted_phase3_attempt(attempt: GenerationAttempt) -> bool:
    try:
        snapshot = json.loads(attempt.request_snapshot)
    except (TypeError, ValueError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    return is_phase3_record(
        backend_id=attempt.backend_id,
        provider_id=attempt.provider_id,
        snapshot=snapshot,
        include_backend_marker=False,
    )


def _engine(database: Path) -> Engine:
    engine = create_engine(f"sqlite:///{database}", future=True)

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
        install_transition_guard(dbapi_connection, connection_record)

    return engine


def _chat(row) -> Chat:
    return Chat(
        id=row.id,
        title=row.title,
        created_at=parse_utc(row.created_at),
        updated_at=parse_utc(row.updated_at),
        head_message_id=row.head_message_id,
        revision=int(row.revision),
    )


def _message(row) -> Message:
    return Message(
        id=row.id,
        chat_id=row.chat_id,
        role=MessageRole(row.role),
        state=MessageState(row.state),
        content=row.content,
        sequence=row.sequence,
        created_at=parse_utc(row.created_at),
        parent_id=row.parent_id,
        lineage_id=row.lineage_id,
        revision=int(row.revision),
        supersedes_id=row.supersedes_id,
    )


def _attempt(row, user_message_content: object | None = None) -> GenerationAttempt:
    token_values = {}
    for field in (
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "total_tokens",
    ):
        value = getattr(row, field)
        token_values[field] = value
    cost = None
    if row.known_cost_usd is not None:
        try:
            cost = Decimal(str(row.known_cost_usd))
        except (InvalidOperation, ValueError):
            raise StateError(f"generation attempt has invalid cost: {row.id}") from None
    attempt = GenerationAttempt(
        id=row.id,
        chat_id=row.chat_id,
        user_message_id=row.user_message_id,
        assistant_message_id=row.assistant_message_id,
        backend_id=row.backend_id,
        model=row.model,
        state=AttemptState(row.state),
        request_snapshot=row.request_snapshot,
        started_at=parse_utc(row.started_at),
        ended_at=None if row.ended_at is None else parse_utc(row.ended_at),
        error_type=row.error_type,
        error_message=row.error_message,
        provider_id=row.provider_id,
        returned_model=row.returned_model,
        request_id=row.request_id,
        finish_reason=row.finish_reason,
        prompt_tokens=token_values["prompt_tokens"],
        completion_tokens=token_values["completion_tokens"],
        reasoning_tokens=token_values["reasoning_tokens"],
        total_tokens=token_values["total_tokens"],
        known_cost_usd=cost,
        remote_outcome_unknown=(
            None if row.remote_outcome_unknown is None else bool(row.remote_outcome_unknown)
        ),
    )
    persisted_phase3 = _is_persisted_phase3_attempt(attempt)
    _validate_request_snapshot(
        attempt,
        user_message_content,
        phase3=persisted_phase3,
    )
    _validate_attempt_outcome(attempt, phase3=persisted_phase3)
    return attempt


def _message_values(message: Message) -> dict[str, object]:
    return {
        "id": message.id,
        "chat_id": message.chat_id,
        "parent_id": message.parent_id,
        "sequence": message.sequence,
        "role": message.role.value,
        "state": message.state.value,
        "content": message.content,
        "created_at": utc_iso(message.created_at),
        "lineage_id": message.lineage_id or message.id,
        "revision": message.revision,
        "supersedes_id": message.supersedes_id,
    }


def _workspace_window(row) -> WorkspaceWindowState:
    mapping = row._mapping
    geometry_value = mapping["geometry_json"]
    geometry: tuple[int, int, int, int] | None
    if geometry_value is None:
        geometry = None
    else:
        try:
            decoded = json.loads(geometry_value)
        except (TypeError, ValueError) as exc:
            raise StateError("workspace geometry is not valid JSON") from exc
        if (
            not isinstance(decoded, list)
            or len(decoded) != 4
            or not all(type(value) is int for value in decoded)
        ):
            raise StateError("workspace geometry must contain four integers")
        geometry = tuple(decoded)  # type: ignore[assignment]
    try:
        return WorkspaceWindowState(
            window_id=str(mapping["window_id"]),
            ordinal=int(mapping["ordinal"]),
            geometry=geometry,
            selected_chat_id=mapping["selected_chat_id"],
            rail_collapsed=bool(mapping["rail_collapsed"]),
            restore_open=bool(mapping["restore_open"]),
            updated_at=parse_utc(mapping["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError("workspace window state is malformed") from exc


def _validate_phase4_schema(connection) -> None:
    table_exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'workspace_windows'"
    ).first()
    if table_exists is None:
        raise RuntimeError("current Phase 4 schema is missing workspace_windows")

    workspace_columns = {
        row[1]: row
        for row in connection.exec_driver_sql("PRAGMA table_info(workspace_windows)").fetchall()
    }
    missing_columns = sorted(set(_PHASE4_WORKSPACE_COLUMN_TYPES) - set(workspace_columns))
    if missing_columns:
        raise RuntimeError(
            "current Phase 4 workspace_windows schema is missing columns: "
            + ", ".join(missing_columns)
        )
    wrong_columns = sorted(
        name
        for name, expected_type in _PHASE4_WORKSPACE_COLUMN_TYPES.items()
        if str(workspace_columns[name][2]).upper().replace(" ", "")
        != expected_type
    )
    if wrong_columns:
        details = ", ".join(
            f"{name}={workspace_columns[name][2]}" for name in wrong_columns
        )
        raise RuntimeError(
            "current Phase 4 workspace_windows columns have invalid declared types: "
            + details
        )
    wrong_nullability = sorted(
        name
        for name in _PHASE4_WORKSPACE_NOT_NULL
        if workspace_columns[name][3] != 1
    )
    if wrong_nullability:
        raise RuntimeError(
            "current Phase 4 workspace_windows columns must be non-null: "
            + ", ".join(wrong_nullability)
        )
    if workspace_columns["window_id"][5] != 1:
        raise RuntimeError("current Phase 4 workspace_windows must use window_id as its primary key")

    foreign_keys = connection.exec_driver_sql(
        "PRAGMA foreign_key_list(workspace_windows)"
    ).fetchall()
    if not any(
        row[2] == "chats"
        and row[3] == "selected_chat_id"
        and row[4] == "id"
        and str(row[6]).upper() == "SET NULL"
        for row in foreign_keys
    ):
        raise RuntimeError(
            "current Phase 4 workspace_windows is missing the selected_chat_id chat reference"
        )

    index_rows = connection.exec_driver_sql(
        "PRAGMA index_list(generation_attempts)"
    ).fetchall()
    active_index = next(
        (row for row in index_rows if row[1] == "ux_generation_attempts_active_chat"),
        None,
    )
    if active_index is None:
        raise RuntimeError("current Phase 4 schema is missing the active-chat uniqueness index")
    if active_index[2] != 1 or active_index[4] != 1:
        raise RuntimeError("current Phase 4 active-chat index must be unique and partial")
    index_columns = connection.exec_driver_sql(
        "PRAGMA index_info(ux_generation_attempts_active_chat)"
    ).fetchall()
    if [(row[0], row[2]) for row in index_columns] != [(0, "chat_id")]:
        raise RuntimeError(
            "current Phase 4 active-chat index must cover only generation_attempts.chat_id"
        )
    index_sql = connection.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND name = 'ux_generation_attempts_active_chat'"
        )
    ).scalar_one_or_none()
    normalized_index_sql = " ".join(str(index_sql or "").strip().rstrip(";").split())
    where_match = re.search(r"\bwhere\b(?P<predicate>.*)\Z", normalized_index_sql, re.IGNORECASE)
    predicate = where_match.group("predicate").strip() if where_match else ""
    if not _PHASE4_ACTIVE_INDEX_PREDICATE.fullmatch(predicate):
        raise RuntimeError(
            "current Phase 4 active-chat index must be filtered to running attempts"
        )


class SQLiteAppStateStore:
    def __init__(self, engine: Engine):
        self._engine = engine
        self._closed = False

    @classmethod
    def open(cls, database: Path) -> SQLiteAppStateStore:
        engine = _engine(database)
        try:
            with engine.begin() as connection:
                integrity = __import__(
                    "bots5.infrastructure.persistence.migrations.versions.0003_integrity_boundaries",
                    fromlist=["_validate_existing_state"],
                )
                integrity._validate_existing_state(connection)
                column_info = {
                    row[1]: row
                    for row in connection.exec_driver_sql(
                        "PRAGMA table_info(generation_attempts)"
                    ).fetchall()
                }
                columns = set(column_info)
                revision = connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar_one_or_none()
                phase3_columns = {
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
                if revision in {"0005_generation_outcomes", "0006_phase4_workspace"} and not phase3_columns <= columns:
                    missing = ", ".join(sorted(phase3_columns - columns))
                    raise RuntimeError(
                        "current Phase 3 schema is missing generation outcome columns: "
                        f"{missing}"
                    )
                if revision in {"0005_generation_outcomes", "0006_phase4_workspace"} or "provider_id" in columns:
                    non_nullable = sorted(
                        name for name in phase3_columns if column_info[name][3] != 0
                    )
                    if non_nullable:
                        raise RuntimeError(
                            "current Phase 3 schema outcome columns must be nullable: "
                            + ", ".join(non_nullable)
                        )
                    wrong_types = sorted(
                        name
                        for name, expected_type in _PHASE3_COLUMN_TYPES.items()
                        if str(column_info[name][2]).upper().replace(" ", "")
                        != expected_type
                    )
                    if wrong_types:
                        details = ", ".join(
                            f"{name}={column_info[name][2]}"
                            for name in wrong_types
                        )
                        raise RuntimeError(
                            "current Phase 3 schema outcome columns have invalid declared types: "
                            + details
                        )
                if revision == "0006_phase4_workspace":
                    _validate_phase4_schema(connection)
                if "provider_id" in columns:
                    outcomes = __import__(
                        "bots5.infrastructure.persistence.migrations.versions.0005_generation_outcomes",
                        fromlist=["_validate_outcome_rows", "_replace_attempt_triggers"],
                    )
                    outcomes._validate_outcome_rows(connection)
                    outcomes._replace_attempt_triggers(connection)
        except BaseException:
            engine.dispose()
            raise
        return cls(engine)

    @property
    def engine(self) -> Engine:
        self._ensure_open()
        return self._engine

    def _ensure_open(self) -> None:
        if self._closed:
            raise StateError("state store is closed")

    def create_chat(self, chat: Chat) -> None:
        self._ensure_open()
        if chat.revision != 0 or chat.head_message_id is not None:
            raise StateError("new chats must start at revision 0 without a head")
        with self._engine.begin() as connection:
            connection.execute(
                insert(chats).values(
                    id=chat.id,
                    title=chat.title,
                    created_at=utc_iso(chat.created_at),
                    updated_at=utc_iso(chat.updated_at),
                    head_message_id=chat.head_message_id,
                    revision=chat.revision,
                )
            )

    def list_chats(self) -> tuple[Chat, ...]:
        self._ensure_open()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(chats).order_by(chats.c.updated_at.desc(), chats.c.id.desc())
            ).fetchall()
        return tuple(_chat(row) for row in rows)

    def get_chat(self, chat_id: str) -> Chat | None:
        self._ensure_open()
        with self._engine.connect() as connection:
            row = connection.execute(select(chats).where(chats.c.id == chat_id)).first()
        return None if row is None else _chat(row)

    def get_message(self, message_id: str) -> Message | None:
        self._ensure_open()
        with self._engine.connect() as connection:
            row = connection.execute(select(messages).where(messages.c.id == message_id)).first()
        return None if row is None else _message(row)

    def list_messages(self, chat_id: str) -> tuple[Message, ...]:
        self._ensure_open()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(messages)
                .where(messages.c.chat_id == chat_id)
                .order_by(messages.c.sequence.asc())
            ).fetchall()
        return tuple(_message(row) for row in rows)

    def list_branch_messages(
        self,
        chat_id: str,
        leaf_message_id: str | None = None,
    ) -> tuple[Message, ...]:
        self._ensure_open()
        chat = self.get_chat(chat_id)
        if chat is None:
            raise StateError(f"chat not found: {chat_id}")
        leaf_id = leaf_message_id or chat.head_message_id
        if leaf_id is None:
            return ()

        by_id = {message.id: message for message in self.list_messages(chat_id)}
        current_id = leaf_id
        branch: list[Message] = []
        seen: set[str] = set()
        while current_id is not None:
            if current_id in seen:
                raise StateError("message lineage contains a cycle")
            seen.add(current_id)
            message = by_id.get(current_id)
            if message is None:
                raise StateError(f"message is not in chat: {current_id}")
            branch.append(message)
            current_id = message.parent_id
        branch.reverse()
        return tuple(branch)

    def list_revisions(self, chat_id: str, lineage_id: str) -> tuple[Message, ...]:
        self._ensure_open()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(messages)
                .where(messages.c.chat_id == chat_id, messages.c.lineage_id == lineage_id)
                .order_by(messages.c.revision.asc(), messages.c.sequence.asc())
            ).fetchall()
        return tuple(_message(row) for row in rows)

    def list_generation_attempts(self, chat_id: str) -> tuple[GenerationAttempt, ...]:
        self._ensure_open()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    generation_attempts,
                    messages.c.id.label("_attempt_user_message_id"),
                    messages.c.content.label("_attempt_user_message_content"),
                )
                .select_from(
                    generation_attempts.outerjoin(
                        messages,
                        messages.c.id == generation_attempts.c.user_message_id,
                    )
                )
                .where(generation_attempts.c.chat_id == chat_id)
                .order_by(
                    generation_attempts.c.started_at.asc(),
                    generation_attempts.c.id.asc(),
                )
            ).fetchall()
        attempts = []
        for row in rows:
            mapping = row._mapping
            if mapping["_attempt_user_message_id"] is None:
                raise StateError(
                    "generation attempt user message not found: "
                    f"{mapping['id']}"
                )
            attempts.append(
                _attempt(row, mapping["_attempt_user_message_content"])
            )
        return tuple(attempts)

    def list_active_generation_attempts(
        self,
        chat_id: str | None = None,
    ) -> tuple[GenerationAttempt, ...]:
        self._ensure_open()
        statement = (
            select(
                generation_attempts,
                messages.c.id.label("_attempt_user_message_id"),
                messages.c.content.label("_attempt_user_message_content"),
            )
            .select_from(
                generation_attempts.join(
                    messages,
                    messages.c.id == generation_attempts.c.user_message_id,
                )
            )
            .where(generation_attempts.c.state == AttemptState.RUNNING.value)
            .order_by(generation_attempts.c.started_at.asc(), generation_attempts.c.id.asc())
        )
        if chat_id is not None:
            statement = statement.where(generation_attempts.c.chat_id == chat_id)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).fetchall()
        return tuple(
            _attempt(row, row._mapping["_attempt_user_message_content"])
            for row in rows
        )

    def get_generation_attempt(self, attempt_id: str) -> GenerationAttempt | None:
        self._ensure_open()
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    generation_attempts,
                    messages.c.id.label("_attempt_user_message_id"),
                    messages.c.content.label("_attempt_user_message_content"),
                )
                .select_from(
                    generation_attempts.outerjoin(
                        messages,
                        messages.c.id == generation_attempts.c.user_message_id,
                    )
                )
                .where(generation_attempts.c.id == attempt_id)
            ).first()
        if row is None:
            return None
        mapping = row._mapping
        if mapping["_attempt_user_message_id"] is None:
            raise StateError(f"generation attempt user message not found: {attempt_id}")
        return _attempt(row, mapping["_attempt_user_message_content"])

    def next_message_sequence(self, chat_id: str) -> int:
        self._ensure_open()
        with self._engine.connect() as connection:
            value = connection.execute(
                select(func.max(messages.c.sequence)).where(messages.c.chat_id == chat_id)
            ).scalar_one()
        return 1 if value is None else int(value) + 1

    def _insert_messages_and_attempt(
        self,
        connection,
        messages_to_insert: tuple[Message, ...],
        attempt: GenerationAttempt,
    ) -> None:
        if attempt.state is not AttemptState.RUNNING or attempt.ended_at is not None:
            raise StateError("generation start must use a running attempt without an end time")
        pending = {message.id: message for message in messages_to_insert}
        if len(pending) != len(messages_to_insert):
            raise StateError("generation start contains duplicate message identities")
        for message in messages_to_insert:
            if message.chat_id != attempt.chat_id:
                raise StateError("generation messages must belong to the attempt chat")
            if message.sequence < 1:
                raise StateError("message sequence must be positive")
            if not isinstance(message.role, MessageRole):
                raise StateError("message role is invalid")
            if message.state.value not in _MESSAGE_STATES:
                raise StateError("message state is invalid")
            if message.supersedes_id is None:
                if message.revision != 1:
                    raise StateError(
                        "a message without a superseded revision must have revision 1"
                    )
                continue
            target = pending.get(message.supersedes_id)
            if target is None:
                target = connection.execute(
                    select(messages).where(messages.c.id == message.supersedes_id)
                ).first()
            if target is None:
                raise StateError(f"superseded message not found: {message.supersedes_id}")
            target_lineage = (
                target.lineage_id if isinstance(target, Message) else target.lineage_id
            )
            target_revision = int(
                target.revision if isinstance(target, Message) else target.revision
            )
            target_chat_id = target.chat_id if isinstance(target, Message) else target.chat_id
            target_role = target.role if isinstance(target, Message) else MessageRole(target.role)
            if target_chat_id != message.chat_id:
                raise StateError("superseded message must be in the same chat")
            if target_lineage != message.lineage_id:
                raise StateError("superseded message must share the message lineage")
            if message.revision != target_revision + 1:
                raise StateError("message revision must immediately follow its superseded revision")
            if target_role != message.role:
                raise StateError("superseded message must have the same role")

        def pending_or_stored(message_id: str) -> Message | None:
            message = pending.get(message_id)
            if message is not None:
                return message
            row = connection.execute(
                select(messages).where(messages.c.id == message_id)
            ).first()
            return None if row is None else _message(row)

        user_message = pending_or_stored(attempt.user_message_id)
        assistant_message = pending_or_stored(attempt.assistant_message_id)
        if user_message is None or user_message.role != MessageRole.USER:
            raise StateError("generation attempt must reference a user message")
        if assistant_message is None or assistant_message.role != MessageRole.ASSISTANT:
            raise StateError("generation attempt must reference an assistant message")
        if user_message.state != MessageState.SENT:
            raise StateError("generation attempt user message must be sent")
        if assistant_message.state != MessageState.STREAMING:
            raise StateError("generation attempt assistant message must be streaming")
        if user_message.chat_id != attempt.chat_id or assistant_message.chat_id != attempt.chat_id:
            raise StateError("generation attempt messages must belong to the attempt chat")
        if attempt.user_message_id == attempt.assistant_message_id:
            raise StateError("generation attempt user and assistant messages must differ")
        if assistant_message.parent_id != user_message.id:
            raise StateError("generation attempt assistant must belong to its user turn")
        _validate_request_snapshot(attempt, user_message.content)
        _validate_attempt_outcome(attempt)

        active_id = connection.execute(
            select(generation_attempts.c.id)
            .where(
                generation_attempts.c.chat_id == attempt.chat_id,
                generation_attempts.c.state == AttemptState.RUNNING.value,
            )
            .limit(1)
        ).scalar_one_or_none()
        if active_id is not None:
            raise StateError(
                "chat already has an active generation: "
                f"{attempt.chat_id} ({active_id})"
            )

        arm_transition(
            connection,
            attempt.assistant_message_id,
            attempt.id,
            "start",
            user_message_id=attempt.user_message_id,
        )
        try:
            connection.execute(
                insert(messages),
                [_message_values(message) for message in messages_to_insert],
            )
            connection.execute(
                insert(generation_attempts).values(
                    id=attempt.id,
                    chat_id=attempt.chat_id,
                    user_message_id=attempt.user_message_id,
                    assistant_message_id=attempt.assistant_message_id,
                    backend_id=attempt.backend_id,
                    model=attempt.model,
                    state=attempt.state.value,
                    request_snapshot=attempt.request_snapshot,
                    started_at=utc_iso(attempt.started_at),
                    provider_id=attempt.provider_id,
                    returned_model=attempt.returned_model,
                    request_id=attempt.request_id,
                    finish_reason=attempt.finish_reason,
                    prompt_tokens=attempt.prompt_tokens,
                    completion_tokens=attempt.completion_tokens,
                    reasoning_tokens=attempt.reasoning_tokens,
                    total_tokens=attempt.total_tokens,
                    known_cost_usd=(
                        None
                        if attempt.known_cost_usd is None
                        else str(attempt.known_cost_usd)
                    ),
                    remote_outcome_unknown=attempt.remote_outcome_unknown,
                )
            )
        finally:
            clear_transition(connection)

    def _advance_chat(
        self,
        connection,
        chat: Chat,
        head_message_id: str,
        expected_chat_revision: int | None,
    ) -> None:
        self._ensure_open()
        if chat.revision < 0:
            raise StateError("chat revision must be nonnegative")
        statement = update(chats).where(chats.c.id == chat.id)
        current_revision = connection.execute(
            select(chats.c.revision).where(chats.c.id == chat.id)
        ).scalar_one_or_none()
        if current_revision is None:
            raise StateError(f"chat not found: {chat.id}")
        expected = current_revision if expected_chat_revision is None else expected_chat_revision
        if chat.revision != int(expected) + 1:
            raise RevisionConflict(f"chat revision must advance by one: {chat.id}")
        statement = statement.where(chats.c.revision == expected)
        arm_transition(connection, head_message_id, chat.id, "advance")
        try:
            result = connection.execute(
                statement.values(
                    updated_at=utc_iso(chat.updated_at),
                    head_message_id=head_message_id,
                    revision=chat.revision,
                )
            )
        finally:
            clear_transition(connection)
        if result.rowcount != 1:
            raise RevisionConflict(f"chat revision changed: {chat.id}")

    def persist_generation_start(
        self,
        chat: Chat,
        user_message: Message,
        assistant_message: Message,
        attempt: GenerationAttempt,
        *,
        expected_chat_revision: int | None = None,
    ) -> None:
        self._ensure_open()
        with self._engine.begin() as connection:
            self._insert_messages_and_attempt(
                connection,
                (user_message, assistant_message),
                attempt,
            )
            self._advance_chat(
                connection,
                chat,
                assistant_message.id,
                expected_chat_revision,
            )

    def persist_regeneration_start(
        self,
        chat: Chat,
        assistant_message: Message,
        attempt: GenerationAttempt,
        *,
        expected_chat_revision: int | None = None,
    ) -> None:
        self._ensure_open()
        with self._engine.begin() as connection:
            self._insert_messages_and_attempt(connection, (assistant_message,), attempt)
            self._advance_chat(
                connection,
                chat,
                assistant_message.id,
                expected_chat_revision,
            )

    def update_streaming_message(self, message: Message) -> None:
        self._ensure_open()
        if message.state != MessageState.STREAMING:
            raise StateError("streaming updates must retain the streaming state")
        with self._engine.begin() as connection:
            result = connection.execute(
                update(messages)
                .where(messages.c.id == message.id)
                .values(state=message.state.value, content=message.content)
            )
            if result.rowcount != 1:
                raise StateError(f"message not found or no longer mutable: {message.id}")

    def finalize_generation(self, message: Message, attempt: GenerationAttempt) -> None:
        self._ensure_open()
        persisted_phase3 = _is_persisted_phase3_attempt(attempt)
        _validate_attempt_outcome(attempt, phase3=persisted_phase3)
        if message.state not in _MESSAGE_TERMINAL_STATES:
            raise StateError("finalized message must be terminal")
        if attempt.state not in _ATTEMPT_TERMINAL_STATES:
            raise StateError("finalized attempt must be terminal")
        expected_message_states = {
            _ATTEMPT_TO_MESSAGE_STATE[attempt.state]
        }
        if attempt.state is AttemptState.INCOMPLETE:
            expected_message_states.add(MessageState.TRUNCATED)
        if message.state not in expected_message_states:
            raise StateError("message and attempt terminal states do not match")
        _validate_request_snapshot(attempt, phase3=persisted_phase3)
        with self._engine.begin() as connection:
            stored_message_row = connection.execute(
                select(messages).where(messages.c.id == message.id)
            ).first()
            stored_attempt_row = connection.execute(
                select(generation_attempts).where(generation_attempts.c.id == attempt.id)
            ).first()
            if stored_message_row is None:
                raise StateError(f"message not found or no longer mutable: {message.id}")
            if stored_attempt_row is None:
                raise StateError(f"generation attempt not found: {attempt.id}")
            stored_message = _message(stored_message_row)
            stored_attempt = _attempt(stored_attempt_row)
            validate_remote_outcome_transition(
                old_state=stored_attempt.state.value,
                old_remote_outcome_unknown=stored_attempt.remote_outcome_unknown,
                new_state=attempt.state.value,
                new_remote_outcome_unknown=attempt.remote_outcome_unknown,
                new_finish_reason=attempt.finish_reason,
                phase3=_is_persisted_phase3_attempt(stored_attempt),
                error_type=StateError,
            )
            if stored_message.state != MessageState.STREAMING:
                raise StateError(f"message is not streaming: {message.id}")
            if stored_attempt.state != AttemptState.RUNNING:
                raise StateError(f"generation attempt is not running: {attempt.id}")
            if stored_attempt.assistant_message_id != message.id:
                raise StateError("finalized message does not belong to the attempt")
            if stored_attempt.user_message_id != message.parent_id:
                raise StateError("finalized message has the wrong user turn")
            stored_user_row = connection.execute(
                select(messages).where(messages.c.id == stored_attempt.user_message_id)
            ).first()
            if stored_user_row is None:
                raise StateError(f"generation attempt user message not found: {attempt.id}")
            _validate_request_snapshot(
                attempt,
                stored_user_row.content,
                phase3=persisted_phase3,
            )
            if stored_attempt.chat_id != message.chat_id or attempt.chat_id != message.chat_id:
                raise StateError("finalized message and attempt must share a chat")
            if (
                attempt.user_message_id != stored_attempt.user_message_id
                or attempt.assistant_message_id != stored_attempt.assistant_message_id
                or attempt.backend_id != stored_attempt.backend_id
                or attempt.model != stored_attempt.model
                or attempt.provider_id != stored_attempt.provider_id
                or attempt.request_snapshot != stored_attempt.request_snapshot
                or utc_iso(attempt.started_at) != utc_iso(stored_attempt.started_at)
            ):
                raise StateError("generation request snapshot identity is immutable")
            if message.chat_id != stored_message.chat_id or message.parent_id != stored_message.parent_id:
                raise StateError("message identity is immutable")
            arm_transition(connection, message.id, attempt.id, "finalize")
            try:
                attempt_result = connection.execute(
                    update(generation_attempts)
                    .where(generation_attempts.c.id == attempt.id)
                    .values(
                        state=attempt.state.value,
                        ended_at=None if attempt.ended_at is None else utc_iso(attempt.ended_at),
                        error_type=attempt.error_type,
                        error_message=attempt.error_message,
                        provider_id=attempt.provider_id,
                        returned_model=attempt.returned_model,
                        request_id=attempt.request_id,
                        finish_reason=attempt.finish_reason,
                        prompt_tokens=attempt.prompt_tokens,
                        completion_tokens=attempt.completion_tokens,
                        reasoning_tokens=attempt.reasoning_tokens,
                        total_tokens=attempt.total_tokens,
                        known_cost_usd=(
                            None
                            if attempt.known_cost_usd is None
                            else str(attempt.known_cost_usd)
                        ),
                        remote_outcome_unknown=attempt.remote_outcome_unknown,
                    )
                )
                if attempt_result.rowcount != 1:
                    raise StateError(f"generation attempt not found: {attempt.id}")
                message_result = connection.execute(
                    update(messages)
                    .where(messages.c.id == message.id)
                    .values(state=message.state.value, content=message.content)
                )
                if message_result.rowcount != 1:
                    raise StateError(f"message not found or no longer mutable: {message.id}")
            finally:
                clear_transition(connection)

    def reconcile_interrupted_generations(self, now) -> None:
        """Persist an honest terminal state for work left running by a prior process."""
        self._ensure_open()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(generation_attempts).where(
                    generation_attempts.c.state == AttemptState.RUNNING.value
                )
            ).fetchall()
        for row in rows:
            attempt = _attempt(row)
            message = self.get_message(attempt.assistant_message_id)
            if message is None:
                raise StateError(
                    f"running generation attempt has no assistant message: {attempt.id}"
                )
            if message.state != MessageState.STREAMING:
                raise StateError(
                    f"running generation attempt has non-streaming assistant: {attempt.id}"
                )
            self.finalize_generation(
                replace(message, state=MessageState.ABORTED),
                replace(
                    attempt,
                    state=AttemptState.ABORTED,
                    ended_at=now,
                    error_type="aborted",
                    error_message="generation was interrupted before application restart",
                    remote_outcome_unknown=(
                        True
                        if attempt.remote_outcome_unknown is None
                        else attempt.remote_outcome_unknown
                    ),
                ),
            )

    def update_attempt(self, attempt: GenerationAttempt) -> None:
        self._ensure_open()
        persisted_phase3 = _is_persisted_phase3_attempt(attempt)
        _validate_attempt_outcome(attempt, phase3=persisted_phase3)
        _validate_request_snapshot(attempt, phase3=persisted_phase3)
        with self._engine.begin() as connection:
            stored_row = connection.execute(
                select(generation_attempts).where(generation_attempts.c.id == attempt.id)
            ).first()
            if stored_row is None:
                raise StateError(f"generation attempt not found: {attempt.id}")
            stored = _attempt(stored_row)
            user_row = connection.execute(
                select(messages).where(messages.c.id == stored.user_message_id)
            ).first()
            if user_row is None:
                raise StateError(f"generation attempt user message not found: {attempt.id}")
            _validate_request_snapshot(
                attempt,
                user_row.content,
                phase3=persisted_phase3,
            )
            validate_remote_outcome_transition(
                old_state=stored.state.value,
                old_remote_outcome_unknown=stored.remote_outcome_unknown,
                new_state=attempt.state.value,
                new_remote_outcome_unknown=attempt.remote_outcome_unknown,
                new_finish_reason=attempt.finish_reason,
                phase3=_is_persisted_phase3_attempt(stored),
                error_type=StateError,
            )
            if (
                attempt.chat_id != stored.chat_id
                or attempt.user_message_id != stored.user_message_id
                or attempt.assistant_message_id != stored.assistant_message_id
                or attempt.backend_id != stored.backend_id
                or attempt.model != stored.model
                or attempt.provider_id != stored.provider_id
                or attempt.request_snapshot != stored.request_snapshot
                or utc_iso(attempt.started_at) != utc_iso(stored.started_at)
            ):
                raise StateError("generation request snapshot identity is immutable")
            result = connection.execute(
                update(generation_attempts)
                .where(generation_attempts.c.id == attempt.id)
                .values(
                    state=attempt.state.value,
                    ended_at=None if attempt.ended_at is None else utc_iso(attempt.ended_at),
                    error_type=attempt.error_type,
                    error_message=attempt.error_message,
                    returned_model=attempt.returned_model,
                    request_id=attempt.request_id,
                    finish_reason=attempt.finish_reason,
                    prompt_tokens=attempt.prompt_tokens,
                    completion_tokens=attempt.completion_tokens,
                    reasoning_tokens=attempt.reasoning_tokens,
                    total_tokens=attempt.total_tokens,
                    known_cost_usd=(
                        None
                        if attempt.known_cost_usd is None
                        else str(attempt.known_cost_usd)
                    ),
                    remote_outcome_unknown=attempt.remote_outcome_unknown,
                )
            )
            if result.rowcount != 1:
                raise StateError(f"generation attempt not found: {attempt.id}")

    def list_workspace_windows(self) -> tuple[WorkspaceWindowState, ...]:
        self._ensure_open()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(workspace_windows).order_by(
                    workspace_windows.c.ordinal.asc(),
                    workspace_windows.c.window_id.asc(),
                )
            ).fetchall()
        states: list[WorkspaceWindowState] = []
        for row in rows:
            try:
                states.append(_workspace_window(row))
            except StateError:
                # Presentation corruption must fall back to a fresh window;
                # it must not prevent access to durable chat state.
                continue
        return tuple(states)

    def save_workspace_window(self, state: WorkspaceWindowState) -> None:
        self._ensure_open()
        geometry_json = None if state.geometry is None else json.dumps(list(state.geometry))
        with self._engine.begin() as connection:
            connection.execute(
                delete(workspace_windows).where(
                    workspace_windows.c.window_id == state.window_id
                )
            )
            connection.execute(
                insert(workspace_windows).values(
                    window_id=state.window_id,
                    ordinal=state.ordinal,
                    geometry_json=geometry_json,
                    selected_chat_id=state.selected_chat_id,
                    rail_collapsed=state.rail_collapsed,
                    restore_open=state.restore_open,
                    updated_at=utc_iso(state.updated_at),
                )
            )

    def delete_workspace_window(self, window_id: str) -> None:
        self._ensure_open()
        with self._engine.begin() as connection:
            connection.execute(
                delete(workspace_windows).where(workspace_windows.c.window_id == window_id)
            )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._engine.dispose()
