from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


_STATE_KEY = "bots5_transition_guard"


def install_transition_guard(dbapi_connection: Any, connection_record: Any) -> None:
    state = {
        "phase": None,
        "message_id": None,
        "attempt_id": None,
        "user_message_id": None,
    }

    def internal_transition(message_id: str | None, attempt_id: str | None, phase: str) -> int:
        if state["phase"] == "start":
            if phase == "start-user":
                return int(state["user_message_id"] == message_id)
            return int(
                phase in {"start-message", "start-attempt"}
                and state["message_id"] == message_id
                and (
                    phase == "start-message"
                    or state["attempt_id"] == attempt_id
                )
            )
        if state["phase"] == "finalize":
            return int(
                phase in {"finalize-message", "finalize-attempt"}
                and state["message_id"] == message_id
                and (
                    phase == "finalize-message"
                    or state["attempt_id"] == attempt_id
                )
            )
        if state["phase"] == "advance":
            return int(
                phase == "advance-chat"
                and state["message_id"] == message_id
                and state["attempt_id"] == attempt_id
            )
        return 0

    def valid_timestamp(value: object) -> int:
        if not isinstance(value, str):
            return 0
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return 0
        canonical = parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        return int(canonical == value)

    dbapi_connection.create_function("bots5_internal_transition", 3, internal_transition)
    dbapi_connection.create_function("bots5_valid_timestamp", 1, valid_timestamp)
    connection_record.info[_STATE_KEY] = state


def arm_transition(
    connection: Any,
    message_id: str,
    attempt_id: str,
    phase: str,
    *,
    user_message_id: str | None = None,
) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is None:
        raise RuntimeError("SQLite transition guard is not installed")
    state.update(
        {
            "phase": phase,
            "message_id": message_id,
            "attempt_id": attempt_id,
            "user_message_id": user_message_id,
        }
    )


def clear_transition(connection: Any) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is not None:
        state.update(
            {
                "phase": None,
                "message_id": None,
                "attempt_id": None,
                "user_message_id": None,
            }
        )
