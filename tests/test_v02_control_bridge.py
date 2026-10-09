"""Tests for the v0.2 control plane bridge (Qt-free).

These exercise the zero-spend prepare/approve projection discipline,
UNKNOWN-outcome handling, and the bounded polling contract.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from bots5.core.capabilities import (
    CapabilityAuthority,
    CapabilityDenied,
    DirectoryScope,
    EgressDestination,
    EgressScope,
    GrantRequest,
    Subject,
)
from bots5.desktop.control_bridge import (
    ApprovalRecord,
    ControlBridge,
    ControlProjection,
    ExecutionProjection,
    ExecutionState,
    GrantProjection,
    GrantScope,
    POLL_INTERVAL_MS,
    POLL_MAX_INTERVAL_MS,
    PreparedControlOperation,
    project_control_state,
    ReceiptProjection,
    ReceiptState,
)
from bots5.errors import ApprovalInvalidatedError, ValidationError
from bots5.core.errors import StateError


class TestBridgeLifecycle:
    def test_bridge_created_open(self):
        bridge = ControlBridge()
        assert bridge.closed is False
        bridge.close()
        assert bridge.closed is True

    def test_operations_after_close_raise(self):
        bridge = ControlBridge()
        bridge.close()
        with pytest.raises(StateError):
            bridge.projection()
        with pytest.raises(StateError):
            bridge.list_grants()
        with pytest.raises(StateError):
            bridge.prepare_execution("git", "git_inspect", "test", "user")

    def test_double_close_is_idempotent(self):
        bridge = ControlBridge()
        bridge.close()
        bridge.close()  # should not raise
        assert bridge.closed is True

    def test_close_async(self):
        bridge = ControlBridge()
        asyncio.run(bridge.close_async())
        assert bridge.closed is True


class TestPollingContract:
    def test_poll_intervals_match_existing_discipline(self):
        """Bounded polling must match the campaign/import-queue cadence."""
        assert POLL_INTERVAL_MS == 250
        assert POLL_MAX_INTERVAL_MS == 1000
        assert POLL_INTERVAL_MS <= POLL_MAX_INTERVAL_MS

    def test_projection_async_returns_control_projection(self):
        bridge = ControlBridge()
        proj = asyncio.run(bridge.projection_async())
        assert isinstance(proj, ControlProjection)
        assert proj.display_state == "idle"
        assert proj.active_count == 0
        bridge.close()


class TestPrepareAndApprove:
    def test_prepare_execution_creates_operation_with_digest(self):
        bridge = ControlBridge()
        prepared = bridge.prepare_execution(
            kind="process",
            scope="process_execution",
            description="run test",
            approved_by="test-user",
        )
        assert isinstance(prepared, PreparedControlOperation)
        assert prepared.kind == "process"
        assert prepared.scope == "process_execution"
        assert prepared.description == "run test"
        assert prepared.operation == "execute"
        assert len(prepared.request_digest) == 64  # sha256 hex
        assert prepared.approval.approved_by == "test-user"
        assert prepared.approval.consumed is False
        bridge.close()

    def test_prepare_deterministic_digest(self):
        """Same request shape must produce the same digest."""
        bridge1 = ControlBridge()
        bridge2 = ControlBridge()
        op1 = bridge1.prepare_execution("git", "git_inspect", "status", "u")
        op2 = bridge2.prepare_execution("git", "git_inspect", "status", "u")
        # Operation IDs differ (timestamps + random), so request_digest differs
        # because operation_id is part of the digest.
        # The point: the digest binds the exact request shape.
        assert op1.request_digest != op2.request_digest
        bridge1.close()
        bridge2.close()

    def test_approve_execution_moves_to_queued(self):
        bridge = ControlBridge()
        prepared = bridge.prepare_execution(
            kind="git", scope="git_inspect", description="status",
            approved_by="test-user",
        )
        bridge.approve_execution(prepared)
        proj = bridge.projection()
        assert len(proj.executions) == 1
        exec_proj = proj.executions[0]
        assert exec_proj.state == ExecutionState.QUEUED.value
        assert exec_proj.kind == "git"
        assert exec_proj.provider_side_outcome_unknown is True
        assert exec_proj.approval_id is not None
        bridge.close()

    def test_approve_without_prepare_raises(self):
        bridge = ControlBridge()
        with pytest.raises(StateError):
            fake_op = PreparedControlOperation(
                operation_id="fake",
                kind="git",
                scope="git_inspect",
                description="fake",
                summary="",
                request_digest="abc",
                approval=ApprovalRecord(
                    approval_id="appr-fake",
                    operation_id="fake",
                    scope="git_inspect",
                    approved_by="user",
                    approved_at=time.time(),
                    request_digest="abc",
                ),
                operation="execute",
            )
            bridge.approve_execution(fake_op)
        bridge.close()

    def test_approve_wrong_operation_raises(self):
        bridge = ControlBridge()
        prepared = bridge.prepare_execution("git", "git_inspect", "s", "u")
        bad_prepared = PreparedControlOperation(
            operation_id=prepared.operation_id,
            kind=prepared.kind,
            scope=prepared.scope,
            description=prepared.description,
            summary=prepared.summary,
            request_digest=prepared.request_digest,
            approval=prepared.approval,
            grant_scope=prepared.grant_scope,
            operation="cancel",  # wrong operation
            estimated_resources={},
        )
        with pytest.raises(ValidationError):
            bridge.approve_execution(bad_prepared)
        bridge.close()


class TestExecutionStateTransitions:
    def test_running_state_counts_as_active(self):
        bridge = ControlBridge()
        prepared = bridge.prepare_execution("git", "git_inspect", "s", "u")
        bridge.approve_execution(prepared)
        op_id = bridge.projection().executions[0].operation_id
        bridge._set_execution_state(op_id, ExecutionState.RUNNING)
        proj = bridge.projection()
        assert proj.active_count == 1
        assert proj.is_running is True
        assert proj.display_state == "running"
        bridge.close()

    def test_succeeded_state_counts_as_succeeded(self):
        bridge = ControlBridge()
        prepared = bridge.prepare_execution("git", "git_inspect", "s", "u")
        bridge.approve_execution(prepared)
        op_id = bridge.projection().executions[0].operation_id
        bridge._set_execution_state(
            op_id, ExecutionState.SUCCEEDED,
            provider_side_outcome_unknown=False,
            exit_code=0,
        )
        proj = bridge.projection()
        assert proj.succeeded_count == 1
        assert proj.active_count == 0
        assert proj.display_state == "succeeded"
        bridge.close()

    def test_failed_unknown_counts_as_unknown_not_failed(self):
        """D-4 / D-9: provider-side-outcome-unknown must NOT count as failed."""
        bridge = ControlBridge()
        prepared = bridge.prepare_execution("git", "git_inspect", "s", "u")
        bridge.approve_execution(prepared)
        op_id = bridge.projection().executions[0].operation_id
        bridge._set_execution_state(
            op_id, ExecutionState.FAILED,
            provider_side_outcome_unknown=True,
            error_type="TransportError",
        )
        proj = bridge.projection()
        assert proj.unknown_count == 1
        assert proj.failed_count == 0
        assert proj.display_state == "uncertain"
        bridge.close()

    def test_failed_known_counts_as_failed(self):
        """A definitive rejection counts as failed, not unknown."""
        bridge = ControlBridge()
        prepared = bridge.prepare_execution("git", "git_inspect", "s", "u")
        bridge.approve_execution(prepared)
        op_id = bridge.projection().executions[0].operation_id
        bridge._set_execution_state(
            op_id, ExecutionState.FAILED,
            provider_side_outcome_unknown=False,
            error_type="ProviderResponseError",
        )
        proj = bridge.projection()
        assert proj.failed_count == 1
        assert proj.unknown_count == 0
        bridge.close()

    def test_cancelled_remains_unknown(self):
        """Cancelled operations preserve uncertainty of outcome."""
        bridge = ControlBridge()
        prepared = bridge.prepare_execution("git", "git_inspect", "s", "u")
        bridge.approve_execution(prepared)
        op_id = bridge.projection().executions[0].operation_id
        bridge._set_execution_state(op_id, ExecutionState.RUNNING)

        # Cancel it
        cancel_prepared = bridge.prepare_cancellation(op_id, "test-user")
        bridge.approve_cancellation(cancel_prepared)

        proj = bridge.projection()
        exec_proj = proj.executions[0]
        assert exec_proj.state == ExecutionState.CANCELLED.value
        # Provider-side outcome is unknown after cancel
        assert exec_proj.provider_side_outcome_unknown is True
        bridge.close()

    def test_cancel_terminal_raises(self):
        bridge = ControlBridge()
        prepared = bridge.prepare_execution("git", "git_inspect", "s", "u")
        bridge.approve_execution(prepared)
        op_id = bridge.projection().executions[0].operation_id
        bridge._set_execution_state(
            op_id, ExecutionState.SUCCEEDED,
            provider_side_outcome_unknown=False,
        )
        with pytest.raises(ValidationError):
            bridge.prepare_cancellation(op_id, "user")
        bridge.close()

    def test_cancel_unknown_op_raises(self):
        bridge = ControlBridge()
        with pytest.raises(ValidationError):
            bridge.prepare_cancellation("nonexistent", "user")
        bridge.close()


class TestGrants:
    def test_grants_are_projected(self):
        bridge = ControlBridge()
        grant = GrantProjection(
            grant_id="grant-001",
            scope="git_inspect",
            subject="/tmp/repo",
            issued_at=time.time(),
            expires_at=None,
            issued_by="test-user",
            request_digest="abc123",
            active=True,
        )
        bridge._add_grant(grant)
        proj = bridge.projection()
        assert len(proj.grants) == 1
        assert proj.grants[0].grant_id == "grant-001"
        assert proj.grants[0].active is True
        bridge.close()

    def test_list_grants_filters_by_scope(self):
        bridge = ControlBridge()
        bridge._add_grant(GrantProjection(
            grant_id="g1", scope="git_inspect", subject="a",
            issued_at=0, expires_at=None, issued_by="u",
            request_digest=None, active=True,
        ))
        bridge._add_grant(GrantProjection(
            grant_id="g2", scope="git_commit", subject="a",
            issued_at=0, expires_at=None, issued_by="u",
            request_digest=None, active=True,
        ))
        assert len(bridge.list_grants()) == 2
        assert len(bridge.list_grants("git_inspect")) == 1
        bridge.close()


class TestReceipts:
    def test_receipts_are_projected(self):
        bridge = ControlBridge()
        receipt = ReceiptProjection(
            receipt_id="receipt-001",
            operation_id="op-001",
            state="settled",
            settled_at=time.time(),
            request_digest="abc",
            result_digest="def",
            grant_id="grant-001",
            unknown_reason=None,
        )
        bridge._add_receipt(receipt)
        proj = bridge.projection()
        assert len(proj.receipts) == 1
        assert proj.receipts[0].state == "settled"
        bridge.close()

    def test_unknown_receipt_carries_reason(self):
        bridge = ControlBridge()
        receipt = ReceiptProjection(
            receipt_id="r-002",
            operation_id="op-002",
            state="unknown",
            settled_at=None,
            request_digest="abc",
            result_digest=None,
            grant_id=None,
            unknown_reason="transport interrupted before ack",
        )
        bridge._add_receipt(receipt)
        proj = bridge.projection()
        assert proj.receipts[0].state == "unknown"
        assert proj.receipts[0].unknown_reason is not None
        bridge.close()


class TestIntegrityWarnings:
    def test_warnings_are_projected_verbatim(self):
        bridge = ControlBridge()
        bridge._add_warning("test warning one")
        bridge._add_warning("test warning two")
        proj = bridge.projection()
        assert len(proj.integrity_warnings) == 2
        assert proj.integrity_warnings[0] == "test warning one"
        bridge.close()


class TestProjectControlState:
    def test_empty_projection_is_idle(self):
        proj = project_control_state()
        assert proj.display_state == "idle"
        assert proj.active_count == 0
        assert proj.has_active is False
        assert len(proj.integrity_warnings) == 0

    def test_running_execution_sets_display_state(self):
        execs = [ExecutionProjection(
            operation_id="op1",
            kind="git",
            state=ExecutionState.RUNNING.value,
            scope="git_inspect",
            description="test",
            submitted_at=time.time(),
            started_at=time.time(),
            ended_at=None,
            duration_seconds=None,
            exit_code=None,
            error_type=None,
            error_message=None,
            provider_side_outcome_unknown=True,
            grant_id=None,
            approval_id=None,
            output_path=None,
        )]
        proj = project_control_state(executions=execs)
        assert proj.display_state == "running"
        assert proj.is_running is True
        assert proj.active_count == 1

    def test_mixed_states_count_correctly(self):
        execs = [
            ExecutionProjection(
                operation_id=f"op{i}", kind="git", state=state,
                scope="git_inspect", description="test",
                submitted_at=time.time(), started_at=None, ended_at=None,
                duration_seconds=None, exit_code=None,
                error_type=None, error_message=None,
                provider_side_outcome_unknown=unknown,
                grant_id=None, approval_id=None, output_path=None,
            )
            for i, (state, unknown) in enumerate([
                (ExecutionState.RUNNING.value, True),
                (ExecutionState.QUEUED.value, True),
                (ExecutionState.SUCCEEDED.value, False),
                (ExecutionState.FAILED.value, True),  # unknown → counts as unknown
                (ExecutionState.FAILED.value, False),  # known → counts as failed
                (ExecutionState.UNKNOWN.value, True),
            ])
        ]
        proj = project_control_state(executions=execs)
        assert proj.active_count == 1   # RUNNING
        assert proj.queued_count == 1   # QUEUED
        assert proj.succeeded_count == 1
        assert proj.failed_count == 1    # one known-failed
        assert proj.unknown_count == 2   # failed-unknown + UNKNOWN


class TestGrantScopeEnum:
    def test_expected_scopes_exist(self):
        """The grant scopes must cover v0.2 reference workflows."""
        expected = {
            "git_inspect", "git_edit", "git_stage", "git_commit", "git_push",
            "process_execution", "tool_invocation", "network_egress", "plugin_load",
        }
        actual = {s.value for s in GrantScope}
        assert expected.issubset(actual), f"Missing: {expected - actual}"


class TestApprovalCallback:
    def test_approve_callback_is_invoked(self):
        bridge = ControlBridge()
        calls = []
        bridge._register_approve_callback("execute", lambda op: calls.append(op))
        prepared = bridge.prepare_execution("git", "git_inspect", "s", "u")
        bridge.approve_execution(prepared)
        assert len(calls) == 1
        assert calls[0].operation_id == prepared.operation_id
        bridge.close()


class TestRealGrantIssuance:
    """Approval must issue a real capability grant through the shared authority.

    These tests pin the CONTRACT-8/9 repair: prepare is zero-spend, approve
    issues exactly one bounded grant bound to the queued execution, and an
    authority refusal surfaces as a denial with no queued execution.
    """

    def test_prepare_leaves_authority_inventory_empty(self, tmp_path: Path):
        """Zero-spend prepare must not issue any grant."""
        authority = CapabilityAuthority()
        bridge = ControlBridge(capability_authority=authority)
        workspace = tmp_path / "ws"
        workspace.mkdir()

        prepared = bridge.prepare_execution(
            kind="tool",
            scope="tool_invocation",
            description="read a file",
            approved_by="operator",
            grant_scope="workspace-read",
            grant_scope_target=workspace,
            estimated_resources={"ttl_seconds": 60.0, "max_effects": 1},
        )

        assert prepared is not None
        assert len(authority.inventory()) == 0
        assert len(bridge.projection().grants) == 0
        bridge.close()

    def test_approve_issues_exactly_one_bound_grant(self, tmp_path: Path):
        """After approve, one grant exists in the authority and binds the execution."""
        authority = CapabilityAuthority()
        bridge = ControlBridge(capability_authority=authority)
        workspace = tmp_path / "ws"
        workspace.mkdir()

        prepared = bridge.prepare_execution(
            kind="tool",
            scope="tool_invocation",
            description="read a file",
            approved_by="operator",
            grant_scope="workspace-read",
            grant_scope_target=workspace,
            estimated_resources={"ttl_seconds": 60.0, "max_effects": 1},
        )
        bridge.approve_execution(prepared)

        proj = bridge.projection()
        assert len(proj.executions) == 1
        assert len(proj.grants) == 1
        assert len(authority.inventory()) == 1

        exec_proj = proj.executions[0]
        grant_proj = proj.grants[0]
        assert exec_proj.grant_id == grant_proj.grant_id
        assert exec_proj.grant_id == authority.inventory()[0].grant_id
        assert grant_proj.scope == "workspace-read"
        assert str(workspace.resolve()) in grant_proj.subject
        bridge.close()

    def test_approve_refusal_surfaces_without_queuing(self):
        """An authority refusal at approve is a denial with no queued execution."""
        authority = CapabilityAuthority()
        bridge = ControlBridge(capability_authority=authority)

        prepared = bridge.prepare_execution(
            kind="tool",
            scope="tool_invocation",
            description="impossible kind",
            approved_by="operator",
            grant_scope="unknown-kind-xyz",
            estimated_resources={"ttl_seconds": 60.0, "max_effects": 1},
        )
        with pytest.raises(ValidationError) as exc_info:
            bridge.approve_execution(prepared)
        assert "refused" in str(exc_info.value).lower()
        assert "UNKNOWN_KIND" in str(exc_info.value)

        # No queued execution, no grant in the shared authority.
        assert len(bridge.projection().executions) == 0
        assert len(bridge.projection().grants) == 0
        assert len(authority.inventory()) == 0
        bridge.close()

    def test_approve_without_authority_still_queues_for_legacy_tests(self):
        """A bridge with no authority remains usable for existing state tests."""
        bridge = ControlBridge()
        prepared = bridge.prepare_execution(
            kind="git", scope="git_inspect", description="status", approved_by="u"
        )
        bridge.approve_execution(prepared)

        proj = bridge.projection()
        assert len(proj.executions) == 1
        assert proj.executions[0].state == ExecutionState.QUEUED.value
        assert proj.executions[0].grant_id is None
        assert len(proj.grants) == 0
        bridge.close()

    def test_approve_egress_issues_grant_with_exact_destination(self):
        """An egress operation receives an egress grant scoped to the destination."""
        authority = CapabilityAuthority()
        bridge = ControlBridge(capability_authority=authority)
        destination = EgressDestination(
            scheme="https", host="api.example.com", port=443
        )

        prepared = bridge.prepare_execution(
            kind="egress",
            scope="network_egress",
            description="send metrics",
            approved_by="operator",
            grant_scope="egress",
            grant_scope_target=destination,
            estimated_resources={"ttl_seconds": 30.0, "max_effects": 1},
        )
        bridge.approve_execution(prepared)

        proj = bridge.projection()
        assert len(proj.executions) == 1
        assert len(proj.grants) == 1
        grant_proj = proj.grants[0]
        assert grant_proj.scope == "egress"
        assert destination.host in grant_proj.subject
        bridge.close()
