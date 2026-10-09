"""Durable execution-queue persistence and crash-recovery adapter.

This module provides a SQLite-backed persistence layer for the execution queue
state machine (:mod:`bots5.core.queue_state_machine`).  It records every
state transition and receipt so that a process restart can recover in-flight
executions to a terminal state that reflects what can be proven.

Recovery contract (BLK-06)
--------------------------
An execution whose effect **cannot** be established must recover to terminal
``UNKNOWN`` and MUST NOT be rewritten to success or definite failure.

An execution that **provably** completed stays COMPLETED; a provable failure
stays FAILED.  An execution that was CANCELLED before the crash stays CANCELLED.

Recovery is idempotent: running it twice against the same database produces the
same result without duplication.

Schema
------
``execution_queue_items``
  (id TEXT PRIMARY KEY,
   revision INTEGER NOT NULL CHECK(revision > 0),
   state TEXT NOT NULL CHECK(state IN (...)),
   operation_id TEXT,
   created_at TEXT NOT NULL,
   updated_at TEXT NOT NULL)

``execution_queue_receipts``
  (item_id TEXT PRIMARY KEY REFERENCES execution_queue_items(id),
   started_at TEXT,
   ended_at TEXT,
   duration_seconds REAL,
   error_type TEXT,
   error_message TEXT,
   provider_side_outcome_unknown INTEGER NOT NULL DEFAULT 0,
   prompt_tokens INTEGER,
   completion_tokens INTEGER,
   reasoning_tokens INTEGER,
   total_tokens INTEGER,
   known_cost_usd TEXT)

``execution_queue_recovery_log``
  (id INTEGER PRIMARY KEY AUTOINCREMENT,
   item_id TEXT NOT NULL,
   recovered_at TEXT NOT NULL,
   previous_state TEXT NOT NULL,
   recovery_reason TEXT NOT NULL)
  — records every recovery decision for audit; the table is append-only.

``execution_queue_bridge_bindings``
  (item_id TEXT PRIMARY KEY REFERENCES execution_queue_items(id),
   operation_id TEXT,
   approval_id TEXT,
   grant_id TEXT,
   kind TEXT,
   scope TEXT,
   description TEXT)
  — binds a queue item back to the desktop-bridge approval, the capability
  grant that was issued, and the human-readable operation metadata needed
  to render a :class:`~bots5.desktop.control_bridge.ExecutionProjection`.

"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, UTC
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from bots5.core.queue_state_machine import (
    ExecutionQueueError,
    ExecutionQueueItem,
    ExecutionQueueState,
    ExecutionReceipt,
    OwnedExecutionWorkers,
    terminalise_unknown,
    transition,
    _ALLOWED,
    _TERMINAL,
)


# --- Schema DDL -----------------------------------------------------------------

_DDL = (
    """CREATE TABLE IF NOT EXISTS execution_queue_items (
        id TEXT PRIMARY KEY,
        revision INTEGER NOT NULL CHECK(revision > 0),
        state TEXT NOT NULL CHECK(state IN (
            'pending','queued','running','settling',
            'completed','failed','cancelled','unknown')),
        operation_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_eqi_state ON execution_queue_items(state)",
    """CREATE TABLE IF NOT EXISTS execution_queue_receipts (
        item_id TEXT PRIMARY KEY REFERENCES execution_queue_items(id),
        started_at TEXT,
        ended_at TEXT,
        duration_seconds REAL,
        error_type TEXT,
        error_message TEXT,
        provider_side_outcome_unknown INTEGER NOT NULL DEFAULT 0,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        reasoning_tokens INTEGER,
        total_tokens INTEGER,
        known_cost_usd TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS execution_queue_recovery_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT NOT NULL,
        recovered_at TEXT NOT NULL,
        previous_state TEXT NOT NULL,
        recovery_reason TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_eqrl_item ON execution_queue_recovery_log(item_id)",
    """CREATE TABLE IF NOT EXISTS execution_queue_bridge_bindings (
        item_id TEXT PRIMARY KEY REFERENCES execution_queue_items(id),
        operation_id TEXT,
        approval_id TEXT,
        grant_id TEXT,
        kind TEXT,
        scope TEXT,
        description TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_eqbb_operation_id ON execution_queue_bridge_bindings(operation_id)",
)


# --- Timestamp helper ------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# --- Decimal safe parse ---------------------------------------------------------

def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


# --- Bridge-binding projection row ---------------------------------------------

@dataclass(frozen=True, slots=True)
class ExecutionDurableRow:
    """One execution as projected from durable rows.

    Carries the queue item, its bridge-binding metadata, and the raw
    timestamp strings needed to render an
    :class:`~bots5.desktop.control_bridge.ExecutionProjection`.
    """

    item: ExecutionQueueItem
    operation_id: str
    approval_id: str | None
    grant_id: str | None
    kind: str | None
    scope: str | None
    description: str | None
    created_at: str
    updated_at: str


# --- Converters ----------------------------------------------------------------

def _receipt_from_row(row: sqlite3.Row, state: ExecutionQueueState | None = None) -> ExecutionReceipt:
    d = dict(row)
    # State may be passed from the item row, or stored in the receipt itself
    receipt_state = state or ExecutionQueueState(d["state"])
    return ExecutionReceipt(
        item_id=d["item_id"],
        state=receipt_state,
        started_at=d["started_at"],
        ended_at=d["ended_at"],
        duration_seconds=d["duration_seconds"],
        error_type=d["error_type"],
        error_message=d["error_message"],
        provider_side_outcome_unknown=bool(d["provider_side_outcome_unknown"]),
        prompt_tokens=d["prompt_tokens"],
        completion_tokens=d["completion_tokens"],
        reasoning_tokens=d["reasoning_tokens"],
        total_tokens=d["total_tokens"],
        known_cost_usd=_parse_decimal(d["known_cost_usd"]),
    )


def _item_from_row(row: sqlite3.Row, receipt: ExecutionReceipt | None) -> ExecutionQueueItem:
    d = dict(row)
    return ExecutionQueueItem(
        id=d["id"],
        revision=d["revision"],
        state=ExecutionQueueState(d["state"]),
        operation_id=d["operation_id"],
        receipt=receipt,
    )


def _load_item_row(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    """Load the raw item row (with row factory set) for transition validation."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM execution_queue_items WHERE id=?",
        (item_id,),
    ).fetchone()


def _validate_persist_transition(
    conn: sqlite3.Connection,
    item: ExecutionQueueItem,
) -> None:
    """Fail closed if a persisted write would bypass the state-machine rules.

    An initial write (no prior row) is allowed for any state that the
    in-memory machine permits as a starting point.  Terminal states are
    monotonic: a persisted UNKNOWN/COMPLETED/FAILED/CANCELLED row cannot be
    rewritten to a different terminal state.  All state changes must follow
    ``_ALLOWED`` and must advance the revision.
    """
    if item.receipt is not None and item.receipt.state != item.state:
        raise ExecutionQueueError(
            f"receipt state {item.receipt.state.value!r} does not match "
            f"persisted item state {item.state.value!r} for {item.id!r}"
        )

    row = _load_item_row(conn, item.id)
    if row is None:
        return

    old_state = ExecutionQueueState(row["state"])
    old_revision = row["revision"]

    if old_state in _TERMINAL:
        if item.state != old_state:
            raise ExecutionQueueError(
                f"cannot rewrite terminal {old_state.value!r} queue item {item.id!r}"
            )
        return

    if item.state == old_state:
        if item.revision < old_revision:
            raise ExecutionQueueError(
                f"queue revision regression for item {item.id!r}"
            )
        return

    if item.state not in _ALLOWED[old_state]:
        raise ExecutionQueueError(
            f"transition {old_state.value!r} -> {item.state.value!r} is not allowed "
            f"for queue item {item.id!r}"
        )

    if item.revision <= old_revision:
        raise ExecutionQueueError(
            f"queue revision must advance for transition on item {item.id!r}"
        )


# --- QueuePersistenceStore ------------------------------------------------------

class QueuePersistenceStore:
    """Durable SQLite adapter for the execution queue state machine.

    Parameters
    ----------
    db_path : Path
        Path to the SQLite database file.  The directory must exist.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    # --- Database lifecycle -----------------------------------------------------

    def _init_db(self) -> None:
        with self._lock:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                for ddl in _DDL:
                    conn.execute(ddl)
                conn.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self._db_path,
            isolation_level="DEFERRED",
            timeout=30.0,
        )
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    # --- CRUD ------------------------------------------------------------------

    def _persist_item(
        self,
        conn: sqlite3.Connection,
        item: ExecutionQueueItem,
        now: str,
    ) -> None:
        """Write one item (and any receipt) inside an existing transaction."""
        conn.execute(
            """INSERT INTO execution_queue_items
                   (id, revision, state, operation_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       revision=excluded.revision,
                       state=excluded.state,
                       operation_id=excluded.operation_id,
                       updated_at=excluded.updated_at""",
            (item.id, item.revision, item.state.value, item.operation_id, now, now),
        )
        if item.receipt is not None:
            r = item.receipt
            conn.execute(
                """INSERT INTO execution_queue_receipts
                       (item_id, started_at, ended_at, duration_seconds,
                        error_type, error_message, provider_side_outcome_unknown,
                        prompt_tokens, completion_tokens, reasoning_tokens,
                        total_tokens, known_cost_usd)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(item_id) DO UPDATE SET
                           started_at=excluded.started_at,
                           ended_at=excluded.ended_at,
                           duration_seconds=excluded.duration_seconds,
                           error_type=excluded.error_type,
                           error_message=excluded.error_message,
                           provider_side_outcome_unknown=excluded.provider_side_outcome_unknown,
                           prompt_tokens=excluded.prompt_tokens,
                           completion_tokens=excluded.completion_tokens,
                           reasoning_tokens=excluded.reasoning_tokens,
                           total_tokens=excluded.total_tokens,
                           known_cost_usd=excluded.known_cost_usd""",
                (
                    r.item_id, r.started_at, r.ended_at, r.duration_seconds,
                    r.error_type, r.error_message, int(r.provider_side_outcome_unknown),
                    r.prompt_tokens, r.completion_tokens, r.reasoning_tokens,
                    r.total_tokens, None if r.known_cost_usd is None else str(r.known_cost_usd),
                ),
            )

    def save_item(self, item: ExecutionQueueItem) -> None:
        """Persist or update one queue item.

        The write is validated against the in-memory state-machine transition
        rules and terminal-state monotonicity.  Persistence cannot be used as
        a back door to rewrite an UNKNOWN item or to jump to an illegal state.
        """
        now = _utc_now()
        with self._lock:
            with self._connect() as conn:
                _validate_persist_transition(conn, item)
                self._persist_item(conn, item, now)
                conn.commit()

    def save_item_with_binding(
        self,
        item: ExecutionQueueItem,
        *,
        operation_id: str,
        approval_id: str | None = None,
        grant_id: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        description: str | None = None,
    ) -> None:
        """Persist one queue item and its bridge-binding metadata atomically.

        This is the durable seam used by the desktop bridge on approval: it
        writes the queue item (so crash recovery can find it) and the
        approval/grant/kind/scope/description binding (so any fresh process
        can render an ExecutionProjection) in a single SQLite transaction.
        """
        now = _utc_now()
        with self._lock:
            with self._connect() as conn:
                _validate_persist_transition(conn, item)
                self._persist_item(conn, item, now)
                self._persist_binding(
                    conn, item.id, operation_id, approval_id, grant_id, kind, scope, description
                )
                conn.commit()

    def load_item(self, item_id: str) -> ExecutionQueueItem | None:
        """Load one queue item by id, or None if not found."""
        with self._lock:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM execution_queue_items WHERE id=?",
                    (item_id,),
                ).fetchone()
                if row is None:
                    return None
                receipt_row = conn.execute(
                    "SELECT * FROM execution_queue_receipts WHERE item_id=?",
                    (item_id,),
                ).fetchone()
                item_state = ExecutionQueueState(row["state"])
                receipt = _receipt_from_row(receipt_row, item_state) if receipt_row else None
                return _item_from_row(row, receipt)

    def load_all_items(self) -> list[ExecutionQueueItem]:
        """Load all queue items."""
        with self._lock:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM execution_queue_items ORDER BY created_at"
                ).fetchall()
                items = []
                for row in rows:
                    receipt_row = conn.execute(
                        "SELECT * FROM execution_queue_receipts WHERE item_id=?",
                        (row["id"],),
                    ).fetchone()
                    item_state = ExecutionQueueState(row["state"])
                    receipt = _receipt_from_row(receipt_row, item_state) if receipt_row else None
                    items.append(_item_from_row(row, receipt))
                return items

    def load_items_in_states(self, *states: ExecutionQueueState) -> list[ExecutionQueueItem]:
        """Load items in any of the given states (used for recovery scanning)."""
        placeholders = ",".join("?" * len(states))
        with self._lock:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"SELECT * FROM execution_queue_items WHERE state IN ({placeholders}) ORDER BY created_at",
                    [s.value for s in states],
                ).fetchall()
                items = []
                for row in rows:
                    receipt_row = conn.execute(
                        "SELECT * FROM execution_queue_receipts WHERE item_id=?",
                        (row["id"],),
                    ).fetchone()
                    item_state = ExecutionQueueState(row["state"])
                    receipt = _receipt_from_row(receipt_row, item_state) if receipt_row else None
                    items.append(_item_from_row(row, receipt))
                return items

    # --- Bridge-binding helpers -------------------------------------------------

    def _persist_binding(
        self,
        conn: sqlite3.Connection,
        item_id: str,
        operation_id: str,
        approval_id: str | None,
        grant_id: str | None,
        kind: str | None,
        scope: str | None,
        description: str | None,
    ) -> None:
        """Write or update the bridge-binding row inside an existing transaction."""
        conn.execute(
            """INSERT INTO execution_queue_bridge_bindings
                   (item_id, operation_id, approval_id, grant_id, kind, scope, description)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(item_id) DO UPDATE SET
                       operation_id=excluded.operation_id,
                       approval_id=excluded.approval_id,
                       grant_id=excluded.grant_id,
                       kind=excluded.kind,
                       scope=excluded.scope,
                       description=excluded.description""",
            (item_id, operation_id, approval_id, grant_id, kind, scope, description),
        )

    def save_binding(
        self,
        item_id: str,
        operation_id: str,
        approval_id: str | None,
        grant_id: str | None,
        kind: str | None,
        scope: str | None,
        description: str | None,
    ) -> None:
        """Persist the bridge-binding metadata for one queue item.

        The binding is idempotent on repeated calls with the same ``item_id``.
        """
        with self._lock:
            with self._connect() as conn:
                self._persist_binding(
                    conn, item_id, operation_id, approval_id, grant_id, kind, scope, description
                )
                conn.commit()

    def load_binding(self, item_id: str) -> dict[str, Any] | None:
        """Load the bridge-binding row for one queue item, or None."""
        with self._lock:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM execution_queue_bridge_bindings WHERE item_id=?",
                    (item_id,),
                ).fetchone()
                if row is None:
                    return None
                return dict(row)

    def load_all_bindings(self) -> dict[str, dict[str, Any]]:
        """Load every bridge-binding row keyed by item_id."""
        with self._lock:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM execution_queue_bridge_bindings"
                ).fetchall()
                return {row["item_id"]: dict(row) for row in rows}

    @staticmethod
    def _binding_from_row(binding_row: sqlite3.Row | None) -> dict[str, Any]:
        if binding_row is None:
            return {}
        return dict(binding_row)

    def load_durable_execution_state(self) -> list[ExecutionDurableRow]:
        """Load every execution row plus its bridge binding and timestamps.

        This is the durable source behind the desktop bridge's
        :class:`~bots5.desktop.control_bridge.ExecutionProjection`.
        """
        with self._lock:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                item_rows = conn.execute(
                    "SELECT * FROM execution_queue_items ORDER BY created_at"
                ).fetchall()
                bindings = {
                    row["item_id"]: self._binding_from_row(row)
                    for row in conn.execute(
                        "SELECT * FROM execution_queue_bridge_bindings"
                    ).fetchall()
                }
                rows: list[ExecutionDurableRow] = []
                for item_row in item_rows:
                    item_id = item_row["id"]
                    receipt_row = conn.execute(
                        "SELECT * FROM execution_queue_receipts WHERE item_id=?",
                        (item_id,),
                    ).fetchone()
                    item_state = ExecutionQueueState(item_row["state"])
                    receipt = _receipt_from_row(receipt_row, item_state) if receipt_row else None
                    binding = bindings.get(item_id, {})
                    d = dict(item_row)
                    rows.append(ExecutionDurableRow(
                        item=_item_from_row(item_row, receipt),
                        operation_id=binding.get("operation_id") or d.get("operation_id") or item_id,
                        approval_id=binding.get("approval_id"),
                        grant_id=binding.get("grant_id"),
                        kind=binding.get("kind"),
                        scope=binding.get("scope"),
                        description=binding.get("description"),
                        created_at=d["created_at"],
                        updated_at=d["updated_at"],
                    ))
                return rows

    # --- Recovery ---------------------------------------------------------------

    def recover_in_flight(self) -> list[ExecutionQueueItem]:
        """Recover all in-flight executions after a process restart.

        This is the BLK-06 recovery entry point.  Items that were in
        RUNNING or SETTLING states when the process died are transitioned to
        UNKNOWN because their terminal effect cannot be established.  Items
        that were PENDING or QUEUED can be left for normal re-queuing.

        Recovery is idempotent: the state update and recovery-log insert are
        committed atomically, and items already in terminal states are skipped
        by the scan.  A crash after the state commit leaves the item UNKNOWN,
        so the next recovery pass correctly skips it.

        Returns the list of items that were recovered (with updated state).
        """
        recovered: list[ExecutionQueueItem] = []

        with self._lock:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                rows = conn.execute(
                    "SELECT * FROM execution_queue_items WHERE state IN (?, ?)",
                    (ExecutionQueueState.RUNNING.value, ExecutionQueueState.SETTLING.value),
                ).fetchall()

                for row in rows:
                    item_id = row["id"]
                    previous_state = row["state"]

                    # Load full item with receipt
                    item = self.load_item(item_id)
                    if item is None:
                        continue

                    # Provable terminal states are left alone
                    if item.state in _TERMINAL:
                        continue

                    # For RUNNING items: must go through SETTLING first
                    # (the state machine requires RUNNING -> SETTLING -> UNKNOWN)
                    if item.state is ExecutionQueueState.RUNNING:
                        try:
                            item = transition(item, item.revision, ExecutionQueueState.SETTLING)
                            item = terminalise_unknown(
                                item,
                                item.revision,
                                reason="process terminated unexpectedly during execution",
                            )
                        except Exception:
                            # If transition fails (e.g., race), skip this item
                            continue

                    elif item.state is ExecutionQueueState.SETTLING:
                        try:
                            item = terminalise_unknown(
                                item,
                                item.revision,
                                reason="process terminated during settlement",
                            )
                        except Exception:
                            continue
                    else:
                        # PENDING or QUEUED — not yet started; leave as-is for
                        # normal re-queuing.  They are not UNKNOWN.
                        continue

                    now = _utc_now()

                    # Persist the recovered state BEFORE appending the audit log,
                    # inside a single transaction.  A crash after this commit
                    # leaves the item UNKNOWN; the next recovery scan skips it
                    # because it is no longer in an uncertain state.
                    self._persist_item(conn, item, now)
                    conn.execute(
                        """INSERT INTO execution_queue_recovery_log
                               (item_id, recovered_at, previous_state, recovery_reason)
                               VALUES (?, ?, ?, ?)""",
                        (
                            item_id,
                            now,
                            previous_state,
                            item.receipt.error_message if item.receipt else "unknown",
                        ),
                    )
                    conn.commit()
                    recovered.append(item)

        return recovered

    # --- Idempotent recovery scan ----------------------------------------------

    def has_been_recovered(self, item_id: str) -> bool:
        """Return True if this item has an entry in the recovery log."""
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM execution_queue_recovery_log WHERE item_id=?",
                    (item_id,),
                ).fetchone()
                return row is not None


# --- Convenience factory for tests ----------------------------------------------

import tempfile


def temp_store() -> tuple[QueuePersistenceStore, Path]:
    """Return a store backed by a temporary file and the path.

    Use this when you need to discard the store object but keep the
    database file (e.g., to simulate a process restart with a new store
    opening the same file).
    """
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
        path = Path(tmp.name)
    return QueuePersistenceStore(path), path
