"""Tests for execution-queue crash/restart recovery (BLK-06).

These tests prove that recovery is a real process-boundary operation, not an
in-memory simulation: we persist state, discard the entire store object,
construct a *new* store over the same database file, run recovery, and verify
the recovered states.

Recovery contract
----------------
- UNKNOWN is preserved when truth cannot be established.
- Provable COMPLETED / FAILED / CANCELLED states are not rewritten.
- Recovery is idempotent (running twice produces the same result).
"""

from __future__ import annotations

from datetime import datetime, UTC

import pytest

from bots5.core.queue_state_machine import (
    ExecutionQueueError,
    ExecutionQueueItem,
    ExecutionQueueState,
    ExecutionReceipt,
    terminalise_completed,
    terminalise_failure,
    terminalise_unknown,
    transition,
)
from bots5.core.queue_persistence import QueuePersistenceStore, temp_store


# --- Helpers -------------------------------------------------------------------

def _item(
    item_id: str,
    state: ExecutionQueueState,
    revision: int = 1,
) -> ExecutionQueueItem:
    return ExecutionQueueItem(id=item_id, revision=revision, state=state)


def _receipt(item_id: str, state: ExecutionQueueState, **kwargs) -> ExecutionReceipt:
    return ExecutionReceipt(item_id=item_id, state=state, **kwargs)


# --- Basic persistence ---------------------------------------------------------

class TestPersistenceBasics:
    def test_save_and_load_round_trip(self):
        store, _ = temp_store()
        item = _item("item-1", ExecutionQueueState.PENDING)
        store.save_item(item)

        loaded = store.load_item("item-1")
        assert loaded is not None
        assert loaded.id == "item-1"
        assert loaded.state is ExecutionQueueState.PENDING
        assert loaded.revision == 1
        assert loaded.receipt is None

    def test_save_item_with_receipt(self):
        store, _ = temp_store()
        # Start from SETTLING to properly terminalise to COMPLETED
        item = _item("item-2", ExecutionQueueState.SETTLING)
        item = terminalise_completed(item, 1, prompt_tokens=10, completion_tokens=5)
        store.save_item(item)

        loaded = store.load_item("item-2")
        assert loaded is not None
        assert loaded.state is ExecutionQueueState.COMPLETED
        assert loaded.receipt is not None
        assert loaded.receipt.is_success
        assert loaded.receipt.prompt_tokens == 10
        assert loaded.receipt.completion_tokens == 5

    def test_load_all_items(self):
        store, _ = temp_store()
        for i in range(3):
            store.save_item(_item(f"item-{i}", ExecutionQueueState.PENDING))

        items = store.load_all_items()
        assert len(items) == 3
        ids = {item.id for item in items}
        assert ids == {"item-0", "item-1", "item-2"}

    def test_load_items_in_states(self):
        store, _ = temp_store()
        store.save_item(_item("p", ExecutionQueueState.PENDING))
        store.save_item(_item("q", ExecutionQueueState.QUEUED))
        store.save_item(_item("r", ExecutionQueueState.RUNNING))
        store.save_item(_item("c", ExecutionQueueState.COMPLETED))

        running = store.load_items_in_states(ExecutionQueueState.RUNNING)
        assert len(running) == 1
        assert running[0].id == "r"

        mixed = store.load_items_in_states(
            ExecutionQueueState.PENDING, ExecutionQueueState.QUEUED
        )
        assert len(mixed) == 2
        ids = {item.id for item in mixed}
        assert ids == {"p", "q"}


# --- Process-boundary recovery (the real BLK-06 test) ---------------------------

class TestProcessBoundaryRecovery:
    def test_running_item_becomes_unknown_after_new_store_recovery(self):
        """Prove process-boundary recovery: discard the store, create a new one.

        This is the core BLK-06 proof: we simulate a crash by dropping the
        store object, then create a brand-new store pointing to the same file,
        run recovery, and verify the RUNNING item became UNKNOWN.
        """
        db_path = None
        # Phase 1: original process saves an in-flight RUNNING item
        store1, path = temp_store()
        db_path = path
        running_item = _item("exec-1", ExecutionQueueState.RUNNING)
        store1.save_item(running_item)

        # Simulate crash: we lose the store object but the file persists
        del store1

        # Phase 2: new process starts with a fresh store on the same database
        store2 = QueuePersistenceStore(db_path)

        # Run recovery
        recovered = store2.recover_in_flight()
        assert len(recovered) == 1
        assert recovered[0].id == "exec-1"
        assert recovered[0].state is ExecutionQueueState.UNKNOWN
        assert recovered[0].receipt is not None
        assert recovered[0].receipt.is_unknown
        assert recovered[0].receipt.provider_side_outcome_unknown is True

    def test_settling_item_becomes_unknown_after_recovery(self):
        """Crash during settlement → UNKNOWN."""
        store, path = temp_store()
        settling_item = _item("exec-2", ExecutionQueueState.SETTLING)
        store.save_item(settling_item)

        del store
        store2 = QueuePersistenceStore(path)
        recovered = store2.recover_in_flight()

        assert len(recovered) == 1
        assert recovered[0].id == "exec-2"
        assert recovered[0].state is ExecutionQueueState.UNKNOWN

    def test_completed_item_not_rewritten(self):
        """A provably COMPLETED item stays COMPLETED after recovery."""
        store, path = temp_store()
        completed = terminalise_completed(
            _item("exec-3", ExecutionQueueState.SETTLING),
            1,
            prompt_tokens=100,
        )
        store.save_item(completed)

        del store
        store2 = QueuePersistenceStore(path)
        recovered = store2.recover_in_flight()

        # Nothing recovered because COMPLETED is terminal and not in uncertain states
        assert recovered == []

        # Verify it's still COMPLETED
        item = store2.load_item("exec-3")
        assert item is not None
        assert item.state is ExecutionQueueState.COMPLETED
        assert item.receipt is not None
        assert item.receipt.prompt_tokens == 100

    def test_failed_item_not_rewritten(self):
        """A provably FAILED item stays FAILED after recovery."""
        store, path = temp_store()
        failed = terminalise_failure(
            _item("exec-4", ExecutionQueueState.RUNNING),
            1,
            error_type="timeout",
            error_message="request timed out",
        )
        store.save_item(failed)

        del store
        store2 = QueuePersistenceStore(path)
        recovered = store2.recover_in_flight()

        assert recovered == []

        item = store2.load_item("exec-4")
        assert item is not None
        assert item.state is ExecutionQueueState.FAILED
        assert item.receipt is not None
        assert item.receipt.error_type == "timeout"

    def test_pending_item_left_as_pending(self):
        """PENDING items that never started are left as-is for re-queuing."""
        store, path = temp_store()
        pending = _item("exec-5", ExecutionQueueState.PENDING)
        store.save_item(pending)

        del store
        store2 = QueuePersistenceStore(path)
        recovered = store2.recover_in_flight()

        # PENDING is not in uncertain_states, so nothing recovered
        assert recovered == []

        item = store2.load_item("exec-5")
        assert item is not None
        assert item.state is ExecutionQueueState.PENDING

    def test_queued_item_left_as_queued(self):
        """QUEUED items that never started are left as-is for re-queuing."""
        store, path = temp_store()
        queued = _item("exec-6", ExecutionQueueState.QUEUED)
        store.save_item(queued)

        del store
        store2 = QueuePersistenceStore(path)
        recovered = store2.recover_in_flight()

        assert recovered == []

        item = store2.load_item("exec-6")
        assert item is not None
        assert item.state is ExecutionQueueState.QUEUED

    def test_unknown_item_not_rewritten(self):
        """An already-UNKNOWN item is not rewritten."""
        store, path = temp_store()
        unknown = terminalise_unknown(
            _item("exec-7", ExecutionQueueState.SETTLING),
            1,
            reason="previous crash",
        )
        store.save_item(unknown)

        del store
        store2 = QueuePersistenceStore(path)
        recovered = store2.recover_in_flight()

        assert recovered == []

        item = store2.load_item("exec-7")
        assert item is not None
        assert item.state is ExecutionQueueState.UNKNOWN
        assert item.receipt is not None
        assert item.receipt.provider_side_outcome_unknown is True


# --- Idempotency ----------------------------------------------------------------

class TestRecoveryIdempotency:
    def test_running_recovery_idempotent(self):
        """Running recovery twice produces the same result (no duplication)."""
        store, path = temp_store()
        running_item = _item("exec-8", ExecutionQueueState.RUNNING)
        store.save_item(running_item)
        del store

        store2 = QueuePersistenceStore(path)
        first_recovered = store2.recover_in_flight()
        assert len(first_recovered) == 1

        # Run recovery again on the same store
        second_recovered = store2.recover_in_flight()
        assert len(second_recovered) == 0  # nothing left to recover

        # Verify the item is still UNKNOWN and wasn't duplicated
        item = store2.load_item("exec-8")
        assert item is not None
        assert item.state is ExecutionQueueState.UNKNOWN

    def test_recovery_log_records_every_decision(self):
        """Each recovery decision is recorded in the recovery log."""
        store, path = temp_store()
        store.save_item(_item("exec-A", ExecutionQueueState.RUNNING))
        store.save_item(_item("exec-B", ExecutionQueueState.SETTLING))
        del store

        store2 = QueuePersistenceStore(path)
        recovered = store2.recover_in_flight()
        assert len(recovered) == 2

        # Check recovery log
        with store2._connect() as conn:
            conn.row_factory = sqlite3.Row
            log_rows = conn.execute(
                "SELECT * FROM execution_queue_recovery_log ORDER BY item_id"
            ).fetchall()
            assert len(log_rows) == 2
            assert log_rows[0]["item_id"] == "exec-A"
            assert log_rows[1]["item_id"] == "exec-B"


# --- Multiple items recovery ----------------------------------------------------

class TestMultipleItemRecovery:
    def test_multiple_in_flight_items_recovered(self):
        """Multiple in-flight items are all recovered correctly."""
        store, path = temp_store()

        # Mix of states
        store.save_item(_item("exec-a", ExecutionQueueState.PENDING))
        store.save_item(_item("exec-b", ExecutionQueueState.QUEUED))
        store.save_item(_item("exec-c", ExecutionQueueState.RUNNING))
        store.save_item(_item("exec-d", ExecutionQueueState.SETTLING))
        completed = terminalise_completed(
            _item("exec-e", ExecutionQueueState.SETTLING), 1
        )
        store.save_item(completed)
        del store

        store2 = QueuePersistenceStore(path)
        recovered = store2.recover_in_flight()

        recovered_ids = {item.id for item in recovered}
        assert recovered_ids == {"exec-c", "exec-d"}

        # Verify final states
        for item_id in ["exec-a", "exec-b", "exec-c", "exec-d", "exec-e"]:
            item = store2.load_item(item_id)
            assert item is not None
            if item_id in ("exec-c", "exec-d"):
                assert item.state is ExecutionQueueState.UNKNOWN
            elif item_id == "exec-e":
                assert item.state is ExecutionQueueState.COMPLETED
            else:
                assert item.state in {
                    ExecutionQueueState.PENDING,
                    ExecutionQueueState.QUEUED,
                }


# --- Full lifecycle persistence --------------------------------------------------

class TestFullLifecyclePersistence:
    def test_pending_to_completed_lifecycle(self):
        """Full lifecycle: PENDING → QUEUED → RUNNING → SETTLING → COMPLETED."""
        store, path = temp_store()

        # Start with PENDING
        item = _item("lifecycle-1", ExecutionQueueState.PENDING)
        item = transition(item, 1, ExecutionQueueState.QUEUED)
        store.save_item(item)

        # Move to RUNNING
        item = transition(item, 2, ExecutionQueueState.RUNNING)
        store.save_item(item)

        # Move to SETTLING
        item = transition(item, 3, ExecutionQueueState.SETTLING)
        store.save_item(item)

        # Complete
        item = terminalise_completed(item, 4, prompt_tokens=50)
        store.save_item(item)

        del store
        store2 = QueuePersistenceStore(path)

        # Nothing to recover - it's terminal COMPLETED
        recovered = store2.recover_in_flight()
        assert recovered == []

        final = store2.load_item("lifecycle-1")
        assert final is not None
        assert final.state is ExecutionQueueState.COMPLETED
        assert final.receipt is not None
        assert final.receipt.prompt_tokens == 50

    def test_crash_during_running_transitions_through_settling(self):
        """Recovery correctly transitions RUNNING → SETTLING → UNKNOWN."""
        store, path = temp_store()

        item = _item("crash-1", ExecutionQueueState.PENDING)
        item = transition(item, 1, ExecutionQueueState.QUEUED)
        item = transition(item, 2, ExecutionQueueState.RUNNING)
        store.save_item(item)
        del store

        store2 = QueuePersistenceStore(path)
        recovered = store2.recover_in_flight()

        assert len(recovered) == 1
        assert recovered[0].id == "crash-1"
        assert recovered[0].state is ExecutionQueueState.UNKNOWN
        assert recovered[0].revision > 3  # revision advanced through transitions


# --- Edge cases -----------------------------------------------------------------

class TestRecoveryEdgeCases:
    def test_missing_receipt_on_terminal_item(self):
        """A COMPLETED row without a receipt row is still recovered correctly."""
        store, path = temp_store()

        # Create a COMPLETED item with receipt first
        item = _item("edge-1", ExecutionQueueState.SETTLING)
        item = terminalise_completed(item, 1, prompt_tokens=100)
        store.save_item(item)

        # Simulate corruption: delete just the receipt row
        with store._connect() as conn:
            conn.execute("DELETE FROM execution_queue_receipts WHERE item_id=?", ("edge-1",))
            conn.commit()

        del store
        store2 = QueuePersistenceStore(path)

        # Recovery should not touch it (it's terminal COMPLETED)
        recovered = store2.recover_in_flight()
        assert recovered == []

        final = store2.load_item("edge-1")
        assert final is not None
        assert final.state is ExecutionQueueState.COMPLETED
        # Receipt is missing due to corruption
        assert final.receipt is None

    def test_has_been_recovered_check(self):
        """has_been_recovered returns True after a recovery."""
        store, path = temp_store()
        store.save_item(_item("check-1", ExecutionQueueState.RUNNING))
        del store

        store2 = QueuePersistenceStore(path)
        assert store2.has_been_recovered("check-1") is False

        store2.recover_in_flight()
        assert store2.has_been_recovered("check-1") is True


# --- Recovery ordering / crash-window idempotency ------------------------------

class TestRecoveryCrashWindow:
    def test_recovery_log_before_state_does_not_cause_skip(self):
        """A pre-existing recovery log entry must not skip a still-RUNNING item.

        This simulates the old bug: the log was written before the recovered
        state was saved, a crash occurred in between, and the next recovery
        pass skipped the item because the log was already present.  With the
        fix, recovery relies on the durable state (not the log) for
        idempotency, so the item is correctly terminalised to UNKNOWN.
        """
        store, path = temp_store()
        store.save_item(_item("crash-window", ExecutionQueueState.RUNNING))

        # Simulate a crash that wrote the recovery log but not the state.
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with store._connect() as conn:
            conn.execute(
                """INSERT INTO execution_queue_recovery_log
                       (item_id, recovered_at, previous_state, recovery_reason)
                       VALUES (?, ?, ?, ?)""",
                ("crash-window", now, ExecutionQueueState.RUNNING.value, "crash before save"),
            )
            conn.commit()

        del store
        store2 = QueuePersistenceStore(path)
        recovered = store2.recover_in_flight()

        assert len(recovered) == 1
        item = store2.load_item("crash-window")
        assert item is not None
        assert item.state is ExecutionQueueState.UNKNOWN


# --- Persistence transition enforcement ----------------------------------------

class TestPersistenceTransitionEnforcement:
    def test_save_item_refuses_illegal_transition(self):
        """Persistence rejects a state change the in-memory machine forbids."""
        store, _ = temp_store()
        store.save_item(_item("bad", ExecutionQueueState.PENDING))

        bad = ExecutionQueueItem(
            id="bad",
            revision=2,
            state=ExecutionQueueState.COMPLETED,
        )
        with pytest.raises(ExecutionQueueError, match="not allowed"):
            store.save_item(bad)

    def test_save_item_refuses_to_rewrite_terminal_unknown(self):
        """A terminal UNKNOWN item cannot be rewritten to a definite outcome."""
        store, _ = temp_store()
        unknown = terminalise_unknown(
            _item("u", ExecutionQueueState.SETTLING),
            1,
            reason="previous crash",
        )
        store.save_item(unknown)

        forged = ExecutionQueueItem(
            id="u",
            revision=3,
            state=ExecutionQueueState.COMPLETED,
        )
        with pytest.raises(ExecutionQueueError, match="cannot rewrite terminal"):
            store.save_item(forged)

    def test_save_item_refuses_to_rewrite_terminal_completed(self):
        """A terminal COMPLETED item cannot be rewritten."""
        store, _ = temp_store()
        completed = terminalise_completed(
            _item("c", ExecutionQueueState.SETTLING),
            1,
            prompt_tokens=10,
        )
        store.save_item(completed)

        forged = ExecutionQueueItem(
            id="c",
            revision=2,
            state=ExecutionQueueState.FAILED,
        )
        with pytest.raises(ExecutionQueueError, match="cannot rewrite terminal"):
            store.save_item(forged)


# --- Import for sqlite3.Row (used in recovery log check) ----------------------
import sqlite3
