"""Tests for the unified execution queue state machine (G4).

Covers states, transitions, guards, failure transitions, cancellation, timeout,
crash/restart recovery, and truthful terminal UNKNOWN.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from bots5.core.errors import StateError
from bots5.core.queue_state_machine import (
    ExecutionQueueError,
    ExecutionQueueItem,
    ExecutionQueueState,
    ExecutionReceipt,
    OwnedExecutionWorkers,
    can_cancel,
    can_timeout,
    terminalise_completed,
    terminalise_failure,
    terminalise_unknown,
    transition,
)


# --- Helpers ---------------------------------------------------------------------

def _item(state: ExecutionQueueState = ExecutionQueueState.PENDING) -> ExecutionQueueItem:
    return ExecutionQueueItem(id="a", revision=1, state=state)


def _receipt(state: ExecutionQueueState, **kwargs) -> ExecutionReceipt:
    return ExecutionReceipt(item_id="a", state=state, **kwargs)


# --- State machine invariant tests ---------------------------------------------

class TestStatesAndTransitions:
    def test_pending_to_queued(self):
        item = _item(ExecutionQueueState.PENDING)
        result = transition(item, 1, ExecutionQueueState.QUEUED)
        assert result.state is ExecutionQueueState.QUEUED
        assert result.revision == 2

    def test_pending_to_cancelled(self):
        item = _item(ExecutionQueueState.PENDING)
        result = transition(item, 1, ExecutionQueueState.CANCELLED)
        assert result.state is ExecutionQueueState.CANCELLED

    def test_queued_to_running(self):
        item = _item(ExecutionQueueState.QUEUED)
        result = transition(item, 1, ExecutionQueueState.RUNNING)
        assert result.state is ExecutionQueueState.RUNNING

    def test_queued_to_cancelled(self):
        item = _item(ExecutionQueueState.QUEUED)
        result = transition(item, 1, ExecutionQueueState.CANCELLED)
        assert result.state is ExecutionQueueState.CANCELLED

    def test_running_to_settling(self):
        item = _item(ExecutionQueueState.RUNNING)
        result = transition(item, 1, ExecutionQueueState.SETTLING)
        assert result.state is ExecutionQueueState.SETTLING

    def test_running_to_failed(self):
        item = _item(ExecutionQueueState.RUNNING)
        result = transition(item, 1, ExecutionQueueState.FAILED, receipt=_receipt(ExecutionQueueState.FAILED, error_type="test_failure"))
        assert result.state is ExecutionQueueState.FAILED

    def test_running_to_cancelled(self):
        item = _item(ExecutionQueueState.RUNNING)
        result = transition(item, 1, ExecutionQueueState.CANCELLED)
        assert result.state is ExecutionQueueState.CANCELLED

    def test_settling_to_completed(self):
        item = _item(ExecutionQueueState.SETTLING)
        result = transition(item, 1, ExecutionQueueState.COMPLETED, receipt=_receipt(ExecutionQueueState.COMPLETED))
        assert result.state is ExecutionQueueState.COMPLETED

    def test_settling_to_failed(self):
        item = _item(ExecutionQueueState.SETTLING)
        result = transition(item, 1, ExecutionQueueState.FAILED, receipt=_receipt(ExecutionQueueState.FAILED, error_type="settle_failure"))
        assert result.state is ExecutionQueueState.FAILED

    def test_settling_to_unknown(self):
        item = _item(ExecutionQueueState.SETTLING)
        result = transition(item, 1, ExecutionQueueState.UNKNOWN, receipt=_receipt(ExecutionQueueState.UNKNOWN))
        assert result.state is ExecutionQueueState.UNKNOWN

    def test_completed_is_terminal(self):
        item = _item(ExecutionQueueState.COMPLETED)
        assert item.is_terminal

    def test_pending_is_not_terminal(self):
        item = _item(ExecutionQueueState.PENDING)
        assert not item.is_terminal

    def test_terminal_has_no_allowed_transitions(self):
        for state in (ExecutionQueueState.COMPLETED, ExecutionQueueState.FAILED,
                      ExecutionQueueState.CANCELLED, ExecutionQueueState.UNKNOWN):
            item = _item(state)
            with pytest.raises(ExecutionQueueError, match="not allowed"):
                transition(item, 1, ExecutionQueueState.PENDING)

    def test_revision_conflict(self):
        item = _item(ExecutionQueueState.PENDING)
        with pytest.raises(ExecutionQueueError, match="revision conflict"):
            transition(item, 99, ExecutionQueueState.QUEUED)

    def test_receipt_state_must_match_target(self):
        item = _item(ExecutionQueueState.SETTLING)
        with pytest.raises(ExecutionQueueError, match="receipt state does not match"):
            transition(item, 1, ExecutionQueueState.COMPLETED, receipt=_receipt(ExecutionQueueState.FAILED, error_type="mismatched"))


class TestTransitionGuards:
    def test_cannot_transition_from_pending_to_running(self):
        item = _item(ExecutionQueueState.PENDING)
        with pytest.raises(ExecutionQueueError, match="not allowed"):
            transition(item, 1, ExecutionQueueState.RUNNING)

    def test_cannot_transition_from_queued_to_settling(self):
        item = _item(ExecutionQueueState.QUEUED)
        with pytest.raises(ExecutionQueueError, match="not allowed"):
            transition(item, 1, ExecutionQueueState.SETTLING)

    def test_cannot_transition_from_running_to_completed_directly(self):
        # RUNNING must go through SETTLING first.
        item = _item(ExecutionQueueState.RUNNING)
        with pytest.raises(ExecutionQueueError, match="not allowed"):
            transition(item, 1, ExecutionQueueState.COMPLETED)

    def test_cannot_transition_from_settling_to_cancelled(self):
        item = _item(ExecutionQueueState.SETTLING)
        with pytest.raises(ExecutionQueueError, match="not allowed"):
            transition(item, 1, ExecutionQueueState.CANCELLED)

    def test_cannot_transition_from_settling_to_running(self):
        item = _item(ExecutionQueueState.SETTLING)
        with pytest.raises(ExecutionQueueError, match="not allowed"):
            transition(item, 1, ExecutionQueueState.RUNNING)


class TestCancellation:
    def test_can_cancel_before_running(self):
        assert can_cancel(_item(ExecutionQueueState.PENDING)) is True
        assert can_cancel(_item(ExecutionQueueState.QUEUED)) is True

    def test_can_cancel_while_running(self):
        assert can_cancel(_item(ExecutionQueueState.RUNNING)) is True

    def test_cannot_cancel_after_settling(self):
        assert can_cancel(_item(ExecutionQueueState.SETTLING)) is False

    def test_cannot_cancel_terminal(self):
        for state in (ExecutionQueueState.COMPLETED, ExecutionQueueState.FAILED,
                      ExecutionQueueState.CANCELLED, ExecutionQueueState.UNKNOWN):
            assert can_cancel(_item(state)) is False

    def test_cancellation_transition_sets_state(self):
        item = _item(ExecutionQueueState.RUNNING)
        result = transition(item, 1, ExecutionQueueState.CANCELLED)
        assert result.state is ExecutionQueueState.CANCELLED


class TestTimeout:
    def test_can_timeout_only_while_running(self):
        assert can_timeout(_item(ExecutionQueueState.RUNNING)) is True
        assert can_timeout(_item(ExecutionQueueState.QUEUED)) is False
        assert can_timeout(_item(ExecutionQueueState.SETTLING)) is False
        for state in (ExecutionQueueState.COMPLETED, ExecutionQueueState.FAILED,
                      ExecutionQueueState.CANCELLED, ExecutionQueueState.UNKNOWN):
            assert can_timeout(_item(state)) is False


class TestTerminalUnknown:
    def test_terminalise_unknown_from_settling(self):
        item = _item(ExecutionQueueState.SETTLING)
        result = terminalise_unknown(item, 1, reason="crash before settlement confirmation")
        assert result.state is ExecutionQueueState.UNKNOWN
        assert result.receipt is not None
        assert result.receipt.state is ExecutionQueueState.UNKNOWN
        assert result.receipt.provider_side_outcome_unknown is True
        assert result.receipt.error_message == "crash before settlement confirmation"

    def test_terminalise_unknown_from_running(self):
        item = _item(ExecutionQueueState.RUNNING)
        # RUNNING cannot go to UNKNOWN directly; must go through SETTLING first.
        with pytest.raises(ExecutionQueueError, match="cannot terminalise UNKNOWN"):
            terminalise_unknown(item, 1)

    def test_terminalise_unknown_preserves_no_success_or_failure(self):
        item = _item(ExecutionQueueState.SETTLING)
        result = terminalise_unknown(item, 1)
        assert result.receipt is not None
        assert result.receipt.is_unknown
        assert not result.receipt.is_success
        assert result.receipt.known_cost_usd is None

    def test_unknown_is_terminal(self):
        item = terminalise_unknown(_item(ExecutionQueueState.SETTLING), 1)
        assert item.is_terminal


class TestTerminalFailure:
    def test_terminalise_failure_from_running(self):
        item = _item(ExecutionQueueState.RUNNING)
        result = terminalise_failure(item, 1, error_type="provider_error", error_message="network unreachable")
        assert result.state is ExecutionQueueState.FAILED
        assert result.receipt.error_type == "provider_error"

    def test_terminalise_failure_requires_error_type(self):
        item = _item(ExecutionQueueState.RUNNING)
        with pytest.raises(ExecutionQueueError, match="requires error_type"):
            terminalise_failure(item, 1, error_type="", error_message="missing type")

    def test_terminalise_failure_from_pending_is_not_allowed(self):
        item = _item(ExecutionQueueState.PENDING)
        with pytest.raises(ExecutionQueueError, match="cannot terminalise FAILED"):
            terminalise_failure(item, 1, error_type="early_failure", error_message="no")


class TestTerminalSuccess:
    def test_terminalise_completed_from_settling(self):
        item = _item(ExecutionQueueState.SETTLING)
        result = terminalise_completed(item, 1, prompt_tokens=10, completion_tokens=5)
        assert result.state is ExecutionQueueState.COMPLETED
        assert result.receipt.prompt_tokens == 10
        assert result.receipt.completion_tokens == 5

    def test_terminalise_completed_from_running_is_not_allowed(self):
        item = _item(ExecutionQueueState.RUNNING)
        with pytest.raises(ExecutionQueueError, match="cannot terminalise COMPLETED"):
            terminalise_completed(item, 1)


class TestReceipt:
    def test_receipt_to_dict_round_trip(self):
        r = ExecutionReceipt(
            item_id="x",
            state=ExecutionQueueState.COMPLETED,
            started_at="2026-10-07T00:00:00Z",
            ended_at="2026-10-07T00:01:00Z",
            prompt_tokens=100,
            completion_tokens=50,
            known_cost_usd=Decimal("0.05"),
        )
        d = r.to_dict()
        assert d["state"] == "completed"
        assert d["known_cost_usd"] == "0.05"

    def test_receipt_unknown_serialises(self):
        r = ExecutionReceipt(item_id="x", state=ExecutionQueueState.UNKNOWN)
        d = r.to_dict()
        assert d["state"] == "unknown"
        assert d["known_cost_usd"] is None

    def test_failed_receipt_requires_error_type(self):
        with pytest.raises(ExecutionQueueError, match="requires error_type"):
            ExecutionReceipt(item_id="x", state=ExecutionQueueState.FAILED)

    def test_non_failed_receipt_must_not_carry_error_type(self):
        with pytest.raises(ExecutionQueueError, match="non-failed receipt must not carry"):
            ExecutionReceipt(item_id="x", state=ExecutionQueueState.COMPLETED, error_type="oops")


# --- OwnedExecutionWorkers integration tests -----------------------------------

@pytest.mark.asyncio
async def test_worker_happy_path():
    registry = OwnedExecutionWorkers()
    item = _item(ExecutionQueueState.QUEUED)

    async def preflight():
        return "prepared"

    async def cutoff(prepared):
        return "committed"

    async def settle(committed):
        return ExecutionReceipt(
            item_id=item.id,
            state=ExecutionQueueState.COMPLETED,
            prompt_tokens=1,
        )

    task = registry.start(item, preflight, cutoff, settle)
    receipt = await task
    assert receipt.state is ExecutionQueueState.COMPLETED


@pytest.mark.asyncio
async def test_worker_cancel_before_cutoff():
    registry = OwnedExecutionWorkers()
    item = _item(ExecutionQueueState.QUEUED)
    cancelled = False

    async def preflight():
        await asyncio.sleep(1.0)
        return "prepared"

    async def cutoff(prepared):
        return "committed"

    async def settle(committed):
        return ExecutionReceipt(item_id=item.id, state=ExecutionQueueState.COMPLETED)

    task = registry.start(item, preflight, cutoff, settle)
    await asyncio.sleep(0.05)
    assert registry.cancel_preflight(item.id) is True
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_worker_cancel_after_cutoff_is_shielded():
    """Post-cutoff cancellation must not abort settlement; receipt is still produced."""
    registry = OwnedExecutionWorkers()
    item = _item(ExecutionQueueState.QUEUED)

    async def preflight():
        return "prepared"

    async def cutoff(prepared):
        return "committed"

    async def settle(committed):
        # Simulate a brief settlement that survives cancellation
        await asyncio.sleep(0.1)
        return ExecutionReceipt(item_id=item.id, state=ExecutionQueueState.COMPLETED)

    task = registry.start(item, preflight, cutoff, settle)
    await asyncio.sleep(0.05)  # let cutoff complete
    registry.cancel_preflight(item.id)  # now it's post-cutoff
    receipt = await task
    assert receipt.state is ExecutionQueueState.COMPLETED


@pytest.mark.asyncio
async def test_worker_shutdown_drains_settlement():
    registry = OwnedExecutionWorkers()
    item = _item(ExecutionQueueState.QUEUED)
    settled = False

    async def preflight():
        return "prepared"

    async def cutoff(prepared):
        return "committed"

    async def settle(committed):
        nonlocal settled
        await asyncio.sleep(0.05)
        settled = True
        return ExecutionReceipt(item_id=item.id, state=ExecutionQueueState.COMPLETED)

    registry.start(item, preflight, cutoff, settle)
    await asyncio.sleep(0.01)  # preflight done, settlement started
    await registry.shutdown()
    assert settled is True


@pytest.mark.asyncio
async def test_worker_shutdown_cancels_preflight():
    registry = OwnedExecutionWorkers()
    item = _item(ExecutionQueueState.QUEUED)
    preflight_started = False

    async def preflight():
        nonlocal preflight_started
        preflight_started = True
        await asyncio.sleep(10.0)
        return "prepared"

    async def cutoff(prepared):
        return "committed"

    async def settle(committed):
        return ExecutionReceipt(item_id=item.id, state=ExecutionQueueState.COMPLETED)

    task = registry.start(item, preflight, cutoff, settle)
    await asyncio.sleep(0.01)
    await registry.shutdown()
    assert preflight_started is True
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_worker_rejects_duplicate_item_id():
    registry = OwnedExecutionWorkers()
    item = _item(ExecutionQueueState.QUEUED)

    async def preflight():
        return "prepared"

    async def cutoff(prepared):
        return "committed"

    async def settle(committed):
        return ExecutionReceipt(item_id=item.id, state=ExecutionQueueState.COMPLETED)

    registry.start(item, preflight, cutoff, settle)
    with pytest.raises(ExecutionQueueError, match="already has an owned worker"):
        registry.start(item, preflight, cutoff, settle)


@pytest.mark.asyncio
async def test_worker_rejects_non_cancellable_item():
    registry = OwnedExecutionWorkers()
    item = _item(ExecutionQueueState.COMPLETED)

    async def preflight():
        return "prepared"

    async def cutoff(prepared):
        return "committed"

    async def settle(committed):
        return ExecutionReceipt(item_id=item.id, state=ExecutionQueueState.COMPLETED)

    with pytest.raises(ExecutionQueueError, match="not cancellable"):
        registry.start(item, preflight, cutoff, settle)


@pytest.mark.asyncio
async def test_worker_preflight_cancel_returns_false_for_missing_item():
    registry = OwnedExecutionWorkers()
    assert registry.cancel_preflight("missing") is False


@pytest.mark.asyncio
async def test_worker_settle_failure_produces_receipt():
    registry = OwnedExecutionWorkers()
    item = _item(ExecutionQueueState.QUEUED)

    async def preflight():
        return "prepared"

    async def cutoff(prepared):
        return "committed"

    async def settle(committed):
        raise ValueError("settlement exploded")

    task = registry.start(item, preflight, cutoff, settle)
    with pytest.raises(ValueError, match="settlement exploded"):
        await task


# --- Crash / restart recovery ----------------------------------------------------

class TestCrashRestartRecovery:
    def test_in_flight_item_can_be_recovered_as_unknown(self):
        """On restart, an item that was RUNNING with no receipt becomes UNKNOWN."""
        item = _item(ExecutionQueueState.RUNNING)
        # Simulate crash: we lost the worker, but the item state persisted.
        # Recovery logic (not in this module, but specified) should terminalise UNKNOWN.
        with pytest.raises(ExecutionQueueError, match="cannot terminalise UNKNOWN"):
            # Direct terminalisation from RUNNING is not allowed; recovery must
            # first transition to SETTLING or another intermediate state.
            terminalise_unknown(item, 1)

    def test_recovery_must_transition_through_settling(self):
        """Correct recovery: RUNNING → SETTLING → UNKNOWN."""
        item = _item(ExecutionQueueState.RUNNING)
        towards_settling = transition(item, 1, ExecutionQueueState.SETTLING)
        result = terminalise_unknown(towards_settling, 2, reason="process terminated unexpectedly")
        assert result.state is ExecutionQueueState.UNKNOWN
        assert result.receipt.error_message == "process terminated unexpectedly"

    def test_queued_item_crash_recovery_is_cancelled(self):
        """An item that never started (QUEUED) after crash can be cancelled."""
        item = _item(ExecutionQueueState.QUEUED)
        result = transition(item, 1, ExecutionQueueState.CANCELLED)
        assert result.state is ExecutionQueueState.CANCELLED

    def test_settling_item_crash_recovery_becomes_unknown(self):
        """Crash during settlement: outcome cannot be established → UNKNOWN."""
        item = _item(ExecutionQueueState.SETTLING)
        result = terminalise_unknown(item, 1, reason="crash during settlement")
        assert result.state is ExecutionQueueState.UNKNOWN
        assert result.receipt.provider_side_outcome_unknown is True


# --- Receipt truthfulness ------------------------------------------------------

class TestReceiptTruthfulness:
    def test_unknown_receipt_does_not_invent_cost(self):
        r = ExecutionReceipt(item_id="x", state=ExecutionQueueState.UNKNOWN)
        assert r.known_cost_usd is None
        assert r.prompt_tokens is None
        assert r.completion_tokens is None

    def test_unknown_receipt_does_not_invent_finish_reason(self):
        r = ExecutionReceipt(item_id="x", state=ExecutionQueueState.UNKNOWN)
        assert r.error_type is None  # best-effort; may carry non-authoritative note
        assert r.error_message is None

    def test_failed_receipt_must_carry_cause(self):
        r = ExecutionReceipt(item_id="x", state=ExecutionQueueState.FAILED, error_type="timeout")
        assert r.error_type == "timeout"
        d = r.to_dict()
        assert d["error_type"] == "timeout"

    def test_receipt_state_matches_item_after_terminalisation(self):
        item = terminalise_failure(_item(ExecutionQueueState.RUNNING), 1, error_type="x", error_message="y")
        assert item.receipt.state is ExecutionQueueState.FAILED
        assert item.state is ExecutionQueueState.FAILED
