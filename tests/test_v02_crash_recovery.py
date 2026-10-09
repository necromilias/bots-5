"""Adversarial crash-recovery tests for the v0.2 control-plane bridge.

These tests must FAIL against the pre-fix code because they assert that:

* approval persists a durable binding readable through a *new* store object;
* prepare alone writes nothing durable and issues no capability grant;
* an in-flight execution that died resolves to terminal UNKNOWN;
* an approved-but-never-started execution survives a process restart as pending;
* recovery is idempotent;
* the closed data root contains only classified logs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bots5.bootstrap.desktop import ControlPlaneState, DesktopRuntime, build_runtime
from bots5.core.capabilities import CapabilityAuthority, CapabilityDenied, DenialReason
from bots5.core.queue_persistence import QueuePersistenceStore
from bots5.core.queue_state_machine import (
    ExecutionQueueItem,
    ExecutionQueueState,
    transition,
)
from bots5.desktop.control_bridge import (
    ControlBridge,
    ExecutionState,
)
from bots5.errors import ValidationError


def _build_control_plane_state(db_path: Path) -> ControlPlaneState:
    """Build a fresh ControlPlaneState over the same database file."""
    authority = CapabilityAuthority()
    return ControlPlaneState(
        capability_authority=authority,
        queue_store_path=db_path,
    )


class TestPrepareDoesNotPersist:
    """Zero-spend prepare discipline: no grant, no durable row."""

    def test_prepare_alone_writes_no_queue_row(self, tmp_path: Path):
        db_path = tmp_path / "queue.db"
        authority = CapabilityAuthority()
        state = ControlPlaneState(
            capability_authority=authority,
            queue_store_path=db_path,
        )
        bridge = ControlBridge(
            state_dir=tmp_path,
            state_source=state,
            capability_authority=authority,
        )

        prepared = bridge.prepare_execution(
            kind="tool",
            scope="tool_invocation",
            description="zero-spend read",
            approved_by="operator",
            grant_scope="workspace-read",
            grant_scope_target=tmp_path / "workspace",
            estimated_resources={"ttl_seconds": 60.0, "max_effects": 1},
        )

        # Prepare must not touch durable storage.
        assert prepared is not None
        fresh = QueuePersistenceStore(db_path)
        assert fresh.load_durable_execution_state() == []
        assert fresh.load_item(prepared.operation_id) is None

        # And the authority inventory must remain empty.
        assert len(authority.inventory()) == 0
        assert len(bridge.projection().grants) == 0

        bridge.close()

    def test_prepare_alone_authority_inventory_unchanged(self, tmp_path: Path):
        authority = CapabilityAuthority()
        state = ControlPlaneState(
            capability_authority=authority,
            queue_store_path=tmp_path / "queue.db",
        )
        bridge = ControlBridge(
            state_dir=tmp_path,
            state_source=state,
            capability_authority=authority,
        )
        bridge.prepare_execution(
            kind="git",
            scope="git_inspect",
            description="status",
            approved_by="operator",
        )
        assert authority.audit_trail() == ()
        assert authority.inventory() == ()
        bridge.close()


class TestApprovalPersistsBinding:
    """Approval writes a durable row binding approval → grant → queue item."""

    def test_approval_creates_durable_row(self, tmp_path: Path):
        db_path = tmp_path / "queue.db"
        authority = CapabilityAuthority()
        state = ControlPlaneState(
            capability_authority=authority,
            queue_store_path=db_path,
        )
        bridge = ControlBridge(
            state_dir=tmp_path,
            state_source=state,
            capability_authority=authority,
        )

        prepared = bridge.prepare_execution(
            kind="tool",
            scope="tool_invocation",
            description="durable read",
            approved_by="operator",
            grant_scope="workspace-read",
            grant_scope_target=tmp_path / "workspace",
            estimated_resources={"ttl_seconds": 60.0, "max_effects": 1},
        )
        bridge.approve_execution(prepared)

        # The bridge projection must show the queued execution.
        proj = bridge.projection()
        assert len(proj.executions) == 1
        exec_proj = proj.executions[0]
        assert exec_proj.state == ExecutionState.QUEUED.value
        assert exec_proj.grant_id is not None
        assert exec_proj.approval_id == prepared.approval.approval_id

        # A fresh store over the same file must read the binding back.
        fresh = QueuePersistenceStore(db_path)
        rows = fresh.load_durable_execution_state()
        assert len(rows) == 1
        row = rows[0]
        assert row.operation_id == prepared.operation_id
        assert row.approval_id == prepared.approval.approval_id
        assert row.grant_id == exec_proj.grant_id
        assert row.kind == "tool"
        assert row.scope == "tool_invocation"
        assert row.description == "durable read"
        assert row.item.state is ExecutionQueueState.QUEUED

        bridge.close()

    def test_approval_refusal_does_not_persist(self, tmp_path: Path):
        authority = CapabilityAuthority()
        state = ControlPlaneState(
            capability_authority=authority,
            queue_store_path=tmp_path / "queue.db",
        )
        bridge = ControlBridge(
            state_dir=tmp_path,
            state_source=state,
            capability_authority=authority,
        )
        prepared = bridge.prepare_execution(
            kind="tool",
            scope="tool_invocation",
            description="impossible",
            approved_by="operator",
            grant_scope="unknown-kind-xyz",
            estimated_resources={"ttl_seconds": 60.0, "max_effects": 1},
        )
        with pytest.raises(ValidationError):
            bridge.approve_execution(prepared)

        fresh = QueuePersistenceStore(tmp_path / "queue.db")
        assert fresh.load_durable_execution_state() == []
        bridge.close()


class TestCrashRecovery:
    """Genuine process-boundary recovery: discard in-memory objects, reopen DB."""

    def test_in_flight_execution_recovers_as_unknown(self, tmp_path: Path):
        db_path = tmp_path / "queue.db"
        authority = CapabilityAuthority()
        state = ControlPlaneState(
            capability_authority=authority,
            queue_store_path=db_path,
        )
        bridge = ControlBridge(
            state_dir=tmp_path,
            state_source=state,
            capability_authority=authority,
        )

        prepared = bridge.prepare_execution(
            kind="process",
            scope="process_execution",
            description="long-running process",
            approved_by="operator",
        )
        bridge.approve_execution(prepared)

        # Simulate the executor moving the item to RUNNING and then crashing.
        store = QueuePersistenceStore(db_path)
        item = store.load_item(prepared.operation_id)
        item = transition(item, item.revision, ExecutionQueueState.RUNNING)
        store.save_item(item)

        # Simulate a genuine restart: discard every object and rebuild the stack.
        del bridge
        del state
        del authority

        state2 = _build_control_plane_state(db_path)
        state2.recover_in_flight()
        bridge2 = ControlBridge(state_dir=tmp_path, state_source=state2)

        proj = bridge2.projection()
        assert len(proj.executions) == 1
        exec_proj = proj.executions[0]
        assert exec_proj.state == ExecutionState.UNKNOWN.value
        assert exec_proj.provider_side_outcome_unknown is True
        assert exec_proj.operation_id == prepared.operation_id
        assert exec_proj.approval_id == prepared.approval.approval_id

        bridge2.close()

    def test_recovery_is_idempotent(self, tmp_path: Path):
        db_path = tmp_path / "queue.db"
        authority = CapabilityAuthority()
        state = ControlPlaneState(
            capability_authority=authority,
            queue_store_path=db_path,
        )
        bridge = ControlBridge(
            state_dir=tmp_path,
            state_source=state,
            capability_authority=authority,
        )

        prepared = bridge.prepare_execution(
            kind="process",
            scope="process_execution",
            description="long-running process",
            approved_by="operator",
        )
        bridge.approve_execution(prepared)

        store = QueuePersistenceStore(db_path)
        item = store.load_item(prepared.operation_id)
        item = transition(item, item.revision, ExecutionQueueState.RUNNING)
        store.save_item(item)

        del bridge
        del state
        del authority

        state2 = _build_control_plane_state(db_path)
        first_recovered = state2.recover_in_flight()
        assert len(first_recovered) == 1

        # Build a new bridge and run recovery from the same database.
        bridge2 = ControlBridge(state_dir=tmp_path, state_source=state2)

        proj = bridge2.projection()
        assert len(proj.executions) == 1
        assert proj.executions[0].state == ExecutionState.UNKNOWN.value

        # Re-running recovery must not duplicate or change the row.
        store2 = QueuePersistenceStore(db_path)
        with store2._connect() as conn:
            first_log_count = conn.execute(
                "SELECT COUNT(*) FROM execution_queue_recovery_log"
            ).fetchone()[0]

        second_recovered = state2.recover_in_flight()
        assert second_recovered == []  # nothing left to recover

        proj2 = bridge2.projection()
        assert len(proj2.executions) == 1
        assert proj2.executions[0].state == ExecutionState.UNKNOWN.value

        with store2._connect() as conn:
            second_log_count = conn.execute(
                "SELECT COUNT(*) FROM execution_queue_recovery_log"
            ).fetchone()[0]
        assert second_log_count == first_log_count

        bridge2.close()

    def test_approved_but_never_started_recovers_as_pending(self, tmp_path: Path):
        db_path = tmp_path / "queue.db"
        authority = CapabilityAuthority()
        state = ControlPlaneState(
            capability_authority=authority,
            queue_store_path=db_path,
        )
        bridge = ControlBridge(
            state_dir=tmp_path,
            state_source=state,
            capability_authority=authority,
        )

        prepared = bridge.prepare_execution(
            kind="tool",
            scope="tool_invocation",
            description="never started",
            approved_by="operator",
            grant_scope="workspace-read",
            grant_scope_target=tmp_path / "workspace",
            estimated_resources={"ttl_seconds": 60.0, "max_effects": 1},
        )
        bridge.approve_execution(prepared)

        # The operator approved but the executor never ran. Discard everything.
        del bridge
        del state
        del authority

        # Restart over the same database. No explicit recover call: the
        # projection itself must read the durable QUEUED row, so the work is
        # not lost.
        state2 = _build_control_plane_state(db_path)
        bridge2 = ControlBridge(state_dir=tmp_path, state_source=state2)

        proj = bridge2.projection()
        assert len(proj.executions) == 1
        exec_proj = proj.executions[0]
        assert exec_proj.state in (
            ExecutionState.PENDING.value,
            ExecutionState.QUEUED.value,
        )
        assert exec_proj.operation_id == prepared.operation_id
        assert exec_proj.approval_id == prepared.approval.approval_id
        assert exec_proj.grant_id is not None

        bridge2.close()

    def test_recovery_does_not_rewrite_known_terminal_states(self, tmp_path: Path):
        db_path = tmp_path / "queue.db"
        authority = CapabilityAuthority()
        state = ControlPlaneState(
            capability_authority=authority,
            queue_store_path=db_path,
        )
        bridge = ControlBridge(
            state_dir=tmp_path,
            state_source=state,
            capability_authority=authority,
        )

        prepared = bridge.prepare_execution(
            kind="tool",
            scope="tool_invocation",
            description="terminal tool",
            approved_by="operator",
            grant_scope="workspace-read",
            grant_scope_target=tmp_path / "workspace",
            estimated_resources={"ttl_seconds": 60.0, "max_effects": 1},
        )
        bridge.approve_execution(prepared)

        # Simulate a completed outcome being durably settled.
        from bots5.core.queue_state_machine import terminalise_completed

        store = QueuePersistenceStore(db_path)
        item = store.load_item(prepared.operation_id)
        item = transition(item, item.revision, ExecutionQueueState.RUNNING)
        store.save_item(item)
        item = store.load_item(prepared.operation_id)
        item = transition(item, item.revision, ExecutionQueueState.SETTLING)
        store.save_item(item)
        item = store.load_item(prepared.operation_id)
        item = terminalise_completed(item, item.revision, prompt_tokens=7)
        store.save_item(item)

        # Crash and restart.
        del bridge
        del state
        del authority

        state2 = _build_control_plane_state(db_path)
        state2.recover_in_flight()
        bridge2 = ControlBridge(state_dir=tmp_path, state_source=state2)

        proj = bridge2.projection()
        assert len(proj.executions) == 1
        assert proj.executions[0].state == ExecutionState.SUCCEEDED.value
        bridge2.close()


class TestClosedStateRootInvariant:
    """The closed data-root contract: only logs survive close."""

    def test_state_root_contains_only_logs_after_close(self, tmp_path: Path):
        # Use the real build_runtime composition so this validates the actual
        # close path, not a hand-rolled construction.
        data_root = tmp_path / "data"
        runtime = build_runtime(data_root)

        # Approve one execution so the queue store is populated.
        bridge = runtime._control_bridge_factory(None)
        workspace = runtime.paths.data_root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        prepared = bridge.prepare_execution(
            kind="tool",
            scope="tool_invocation",
            description="close invariant",
            approved_by="operator",
            grant_scope="workspace-read",
            grant_scope_target=workspace,
            estimated_resources={"ttl_seconds": 60.0, "max_effects": 1},
        )
        bridge.approve_execution(prepared)
        asyncio.run(bridge.close_async())

        # Close the runtime; this exercises the bounded control-plane close stage.
        asyncio.run(runtime.close())

        # After close, the data-root state root (logs) must be the only
        # file-system component left; the database directory is under the
        # classified data-root component and is not part of the closed state root.
        state_root = runtime.paths.state_root
        assert state_root.exists(), "state root must exist after close"
        entries = {p.name for p in state_root.iterdir()}
        assert entries == {"logs"}, f"closed state root must contain only logs, got {entries}"

    def test_queue_store_lives_under_database_directory(self, tmp_path: Path):
        data_root = tmp_path / "data"
        runtime = build_runtime(data_root)
        assert runtime.control_plane_state.queue_store_path.parent == runtime.paths.data_root / "database"
        asyncio.run(runtime.close())
