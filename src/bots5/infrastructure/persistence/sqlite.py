from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from sqlalchemy import Engine, create_engine, delete, event, func, insert, select, update

from bots5.core.errors import RevisionConflict, StateError
from bots5.domain.clock import parse_utc, utc_iso
from bots5.domain.models import (
    AttemptState,
    Chat,
    GenerationAttempt,
    Message,
    MessageRole,
    MessageState,
)

from .schema import chats, generation_attempts, messages
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


def _validate_request_snapshot(attempt: GenerationAttempt) -> None:
    try:
        snapshot = json.loads(attempt.request_snapshot)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StateError("generation request snapshot must be valid JSON") from exc
    if not isinstance(snapshot, dict):
        raise StateError("generation request snapshot must be a JSON object")
    expected = {
        "attempt_id": attempt.id,
        "chat_id": attempt.chat_id,
        "user_message_id": attempt.user_message_id,
        "backend_id": attempt.backend_id,
        "model": attempt.model,
    }
    for key, value in expected.items():
        if key in snapshot and snapshot[key] != value:
            raise StateError(f"generation request snapshot contradicts {key}")


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


def _attempt(row) -> GenerationAttempt:
    return GenerationAttempt(
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
    )


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


class SQLiteAppStateStore:
    def __init__(self, engine: Engine):
        self._engine = engine
        self._closed = False

    @classmethod
    def open(cls, database: Path) -> SQLiteAppStateStore:
        engine = _engine(database)
        try:
            with engine.connect() as connection:
                integrity = __import__(
                    "bots5.infrastructure.persistence.migrations.versions.0003_integrity_boundaries",
                    fromlist=["_validate_existing_state"],
                )
                integrity._validate_existing_state(connection)
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
                select(generation_attempts)
                .where(generation_attempts.c.chat_id == chat_id)
                .order_by(
                    generation_attempts.c.started_at.asc(),
                    generation_attempts.c.id.asc(),
                )
            ).fetchall()
        return tuple(_attempt(row) for row in rows)

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
        _validate_request_snapshot(attempt)
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
        _validate_request_snapshot(attempt)
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
            if stored_message.state != MessageState.STREAMING:
                raise StateError(f"message is not streaming: {message.id}")
            if stored_attempt.state != AttemptState.RUNNING:
                raise StateError(f"generation attempt is not running: {attempt.id}")
            if stored_attempt.assistant_message_id != message.id:
                raise StateError("finalized message does not belong to the attempt")
            if stored_attempt.user_message_id != message.parent_id:
                raise StateError("finalized message has the wrong user turn")
            if stored_attempt.chat_id != message.chat_id or attempt.chat_id != message.chat_id:
                raise StateError("finalized message and attempt must share a chat")
            if (
                attempt.user_message_id != stored_attempt.user_message_id
                or attempt.assistant_message_id != stored_attempt.assistant_message_id
                or attempt.backend_id != stored_attempt.backend_id
                or attempt.model != stored_attempt.model
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
                ),
            )

    def update_attempt(self, attempt: GenerationAttempt) -> None:
        self._ensure_open()
        _validate_request_snapshot(attempt)
        with self._engine.begin() as connection:
            result = connection.execute(
                update(generation_attempts)
                .where(generation_attempts.c.id == attempt.id)
                .values(
                    state=attempt.state.value,
                    ended_at=None if attempt.ended_at is None else utc_iso(attempt.ended_at),
                    error_type=attempt.error_type,
                    error_message=attempt.error_message,
                )
            )
            if result.rowcount != 1:
                raise StateError(f"generation attempt not found: {attempt.id}")

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._engine.dispose()
