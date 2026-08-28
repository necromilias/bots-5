from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import StorageError


EVENT_TYPES = {
    "run_started",
    "stage_queued",
    "stage_started",
    "request_sent",
    "stage_succeeded",
    "stage_failed",
    "stage_skipped",
    "synthesis_blocked",
    "run_timed_out",
    "run_succeeded",
    "run_failed",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventWriter:
    """Small synchronous JSONL writer serialized by a process-local lock."""

    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self._lock = threading.Lock()

    def write(self, event: str, stage_id: str | None = None, **meta: Any) -> None:
        if event not in EVENT_TYPES:
            raise StorageError(f"unknown event type: {event}")
        line: dict[str, Any] = {
            "ts": now_iso(),
            "run_id": self.run_id,
            "event": event,
        }
        if stage_id is not None:
            line["stage_id"] = stage_id
        if meta:
            line["meta"] = meta
        try:
            payload = json.dumps(line, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"
            with self._lock:
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
        except (OSError, TypeError, ValueError) as exc:
            raise StorageError(f"cannot append event log: {self.path}: {exc}") from None
