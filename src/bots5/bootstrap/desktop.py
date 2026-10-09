from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from bots5.core.application import (
    ApplicationCloseState,
    BotsApplication,
    GenerationMode,
    TerminalCloseError,
    TerminalCloseResult,
)
from bots5.core.campaign import CampaignBridge
from bots5.core.capabilities import CapabilityAuthority, DirectoryScope
from bots5.core.egress import ControlledEgressConsumer
from bots5.desktop.control_bridge import ControlBridge
from bots5.core.errors import AuthorityError, BackupError, BackupUnclassifiedState, CoreError, StateError
from bots5.core.events import EventBus
from bots5.core.execution import ExecutionManager
from bots5.core.provider_configuration import ProviderConfiguration
from bots5.core.queue_persistence import QueuePersistenceStore
from bots5.core.queue_state_machine import OwnedExecutionWorkers
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.app_paths import AppPaths, resolve_app_paths
from bots5.infrastructure.authority_lock import AuthorityLock
from bots5.infrastructure.backup_capture import RootedBackupCaptureAdapter
from bots5.infrastructure.git_authority import GitAuthorityManager
from bots5.infrastructure.process_execution import (
    BoundedProcessExecutor,
    CodeExecutor,
)
from bots5.infrastructure.backup_package import (
    BackupFilePublicationAdapter,
    BackupZipPackageAdapter,
)
from bots5.core.backup import BackupService
from bots5.domain.backup import canonical_backup_json
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.generation.openai_compatible import OpenAICompatibleStreamingBackend
from bots5.infrastructure.generation.router import BuiltinProviderRouter
from bots5.infrastructure.restore_service import RestoreService
from bots5.infrastructure.secrets import secret_store_for
from bots5.providers.openai_compatible import OpenAICompatibleProvider
from bots5.providers.base import ReasoningEffort
# Qt-free module import: bots5.desktop.session (a QObject subclass) imports
# PySide6, so it is imported where the desktop session is actually composed
# (build_runtime).  The non-UI restore initiation path below must be able to
# run with PySide6 entirely unavailable.
from bots5.desktop.profile import DesktopSessionInfo

# Plugin imports
try:
    from bots5.plugins import PluginHost
except ImportError:
    # Fallback for tests that don't have plugins
    PluginHost = None  # type: ignore


@dataclass(frozen=True, slots=True)
class RestoreHandoffRequest:
    """The one immutable Slice E handoff request (RESTORE_UI_HANDOFF.md §2 S5).

    Carries the verified ``*.botsbackup`` package and the backup id the live
    verification bound, so the pre-store bootstrap child can independently
    re-verify against that identity (defence in depth).  Whole-installation
    restore is never initiated from this live process; this request only
    names the child's ``--restore-from``/``--expected-backup-id`` arguments.
    """

    package: Path
    expected_backup_id: str


@dataclass(frozen=True, slots=True)
class HandoffCloseOutcome:
    """The ``request_orderly_close`` result (RESTORE_UI_HANDOFF.md §2.1)."""

    all_closed: bool  # True -> serve()'s wait_closed()/close path will proceed
    remaining: int  # windows still open at timeout (0 when all_closed)


class RestoreHandoffRegistry:
    """Process-scoped, one-shot restore-handoff request. Owned by DesktopRuntime.

    ``register`` raises :class:`StateError` when a request is already pending,
    so a second restore can never be queued behind the first; ``take``
    consumes atomically; ``clear`` aborts (idempotent).  The registry is
    touched only from the qasync loop thread.
    """

    def __init__(self) -> None:
        self._request: RestoreHandoffRequest | None = None

    def register(self, request: RestoreHandoffRequest) -> None:
        if self._request is not None:
            raise StateError("a restore handoff request is already pending")
        self._request = request

    def take(self) -> RestoreHandoffRequest | None:
        request = self._request
        self._request = None
        return request

    def clear(self) -> None:
        self._request = None

    @property
    def pending(self) -> bool:
        return self._request is not None


#: Bounded-poll cadence for ``request_orderly_close`` (step 3 of §2.1).
_HANDOFF_CLOSE_POLL_SECONDS = 0.05


class RestoreHandoffCapability:
    """The single wired route from the restore dialog to the runtime close.

    Owned by DesktopRuntime; handed to MainWindow at construction; read by
    Phase9DesktopController.  The dialog never touches DesktopRuntime or the
    Qt window lifecycle directly, and there is deliberately no module-level
    singleton (rejected alternative F-05): every route runs through the
    capability a runtime built for itself.

    The request plane (``register``/``take``/``clear``/``pending``) delegates
    to the runtime-owned :class:`RestoreHandoffRegistry`;
    :meth:`request_orderly_close` is the wired close-all operation (F-05).
    """

    def __init__(self, runtime: "DesktopRuntime") -> None:
        self._runtime = runtime

    # ------------------------------------------------------------------
    # Request plane (delegates to the runtime-owned registry)
    # ------------------------------------------------------------------

    def register(self, request: RestoreHandoffRequest) -> None:
        self._runtime._handoff_registry.register(request)

    def take(self) -> RestoreHandoffRequest | None:
        return self._runtime._handoff_registry.take()

    def clear(self) -> None:
        self._runtime._handoff_registry.clear()

    @property
    def pending(self) -> bool:
        return self._runtime._handoff_registry.pending

    # ------------------------------------------------------------------
    # Close plane
    # ------------------------------------------------------------------

    async def request_orderly_close(
        self, *, timeout: float = 10.0
    ) -> HandoffCloseOutcome:
        """Close every open MainWindow and report the outcome (§2.1 verbatim).

        1. snapshot runtime.windows; if empty -> HandoffCloseOutcome(True, 0)
        2. call window.close() on each (GUI thread; this is the SAME close
           path an operator click uses; MainWindow.closeEvent's
           active-generation prompt applies only for the last registered
           window, so it may refuse the LAST window)
        3. bounded-poll (await asyncio.sleep) until runtime.windows is empty
           or timeout
        4. all closed -> HandoffCloseOutcome(True, 0); the existing serve()
           path then resumes at workspace.wait_closed() and runs
           runtime.close()
        5. timeout/decline -> self.clear() (abort the handoff, launch
           nothing) and return HandoffCloseOutcome(False, remaining) so the
           controller can tell the operator that the restore was NOT
           initiated

        Oracle R-1: the awaiting coroutine is owned by the controller's
        coordinator, never by ``MainWindow._schedule`` (whose tasks
        ``stop_bridge()`` cancels as soon as any window's ``_finish_close``
        begins).  If the awaiter is nevertheless cancelled at the polling
        suspension, this coroutine propagates the cancellation WITHOUT
        clearing the request: a close that has already begun may still
        succeed, and the post-close step then consumes the still-pending
        request.  Clearing happens only on the explicit timeout/decline
        path, never on cancellation — the request can only remain pending
        if the close actually succeeded or is still in flight.
        """

        runtime = self._runtime
        windows = tuple(runtime.windows)
        if not windows:
            return HandoffCloseOutcome(all_closed=True, remaining=0)
        for window in windows:
            try:
                window.close()
            except Exception:
                # A window that raises on close simply stays open; the
                # bounded poll below turns that into the declined outcome.
                continue
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while runtime.windows:
            if loop.time() >= deadline:
                remaining = len(runtime.windows)
                # Decline/timeout (H7): abort the handoff so a later normal
                # close never triggers an unexpected restore.
                self.clear()
                return HandoffCloseOutcome(all_closed=False, remaining=remaining)
            await asyncio.sleep(_HANDOFF_CLOSE_POLL_SECONDS)
        return HandoffCloseOutcome(all_closed=True, remaining=0)


#: Phase 10 bounded campaign close budget.  Each hosted campaign is given this
#: long to reach a durable terminal record during shutdown; on expiry the stage
#: is recorded as a close error and never retried.
_CAMPAIGN_CLOSE_TIMEOUT_SECONDS = 30.0


class ControlPlaneState:
    """The composition root's shared v0.2 control-plane state source.

    One instance per ``build_runtime`` call, injected into every desktop
    :class:`~bots5.desktop.control_bridge.ControlBridge`.  The bridge then
    projects *this* object instead of private per-window dicts, so all
    windows observe one common grant/execution/receipt view.

    Truthfulness boundaries (BLK-04, deliberately blunt):

    * ``grants()`` reads the LIVE shared :class:`CapabilityAuthority`
      inventory — that half really is composed with the seam.
    * The durable queue/receipt machinery IS attached: build_runtime opens
      one :class:`~bots5.core.queue_persistence.QueuePersistenceStore` under
      the state root and binds it to one
      :class:`~bots5.core.queue_state_machine.OwnedExecutionWorkers` registry
      via ``attach_queue_store``.  Queue items saved through
      ``persist_execution`` are durable from that moment on.
    * ``executions()`` / ``receipts()`` now read from the durable queue store
      when one is attached.  The in-memory ``_executions`` / ``_receipts``
      dicts are retained only for observations that have not yet been written
      through; bridge-approved executions are recovered directly from the
      database file.  Because the projection genuinely reads durable rows,
      ``durable`` is ``True`` whenever a queue store is attached.
    * The instance is thread-safe only in the sense the desktop needs: it is
      touched from the qasync loop thread.
    """

    def __init__(
        self,
        *,
        capability_authority: CapabilityAuthority,
        queue_store_path: Path | None = None,
    ) -> None:
        self._capability_authority = capability_authority
        self._queue_store_path = queue_store_path
        self._queue_store: QueuePersistenceStore | None = None
        self._execution_workers: OwnedExecutionWorkers | None = None
        if queue_store_path is not None:
            # BLK-06 durability seam: open the SQLite queue store once and
            # bind it to the shared owned-workers registry.  Construction of
            # the runtime therefore DOES create this control-plane database
            # file in the classified data-root database directory (it is
            # control-plane state, not data-root-authoritative content, and
            # the state root must remain free of everything except logs so
            # the desktop close contract can hold).
            self._queue_store = QueuePersistenceStore(queue_store_path)
            self._execution_workers = OwnedExecutionWorkers()
            self._execution_workers.attach_queue_store(self._queue_store)
        self._executions: dict[str, object] = {}
        self._receipts: dict[str, object] = {}
        self._warnings: list[str] = []

    #: True whenever the shared state source is backed by a real SQLite queue
    #: store, because executions()/receipts() then read from durable rows.
    #: A ControlPlaneState with no store (legacy test construction) reports
    #: False and falls back to the in-memory dicts.
    @property
    def durable(self) -> bool:
        return self._queue_store is not None

    @property
    def capability_authority(self) -> CapabilityAuthority:
        return self._capability_authority

    @property
    def queue_store_path(self) -> Path | None:
        return self._queue_store_path

    @property
    def queue_store(self) -> QueuePersistenceStore | None:
        """The attached durable queue-receipt store, or None."""
        return self._queue_store

    @property
    def execution_workers(self) -> OwnedExecutionWorkers | None:
        """The shared owned-execution-worker registry bound to the store."""
        return self._execution_workers

    def persist_execution(
        self,
        item,
        *,
        operation_id: str | None = None,
        approval_id: str | None = None,
        grant_id: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        description: str | None = None,
    ) -> None:
        """DURABLY save one execution-queue item through the attached store.

        When binding metadata is supplied, the queue item and its bridge
        binding (approval → grant → operation) are persisted atomically in
        one SQLite transaction.  Raises ``StateError`` when no queue store
        was composed — persistence is never silently skipped.
        """
        if self._queue_store is None:
            raise StateError("no durable queue store is attached to the control plane")
        if operation_id is not None:
            self._queue_store.save_item_with_binding(
                item,
                operation_id=operation_id,
                approval_id=approval_id,
                grant_id=grant_id,
                kind=kind,
                scope=scope,
                description=description,
            )
        else:
            self._execution_workers.persist_item(item)

    def recover_in_flight(self) -> list:
        """Crash-recover attached queue items (delegates to the store)."""
        if self._execution_workers is None:
            raise StateError("no durable queue store is attached to the control plane")
        return self._execution_workers.recover_in_flight()

    # -- durable-row projection helpers ----------------------------------

    @staticmethod
    def _iso_to_timestamp(iso: str | None) -> float | None:
        if iso is None:
            return None
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()

    def _durable_row_to_execution_projection(self, row) -> object:
        """Map one durable :class:`~bots5.core.queue_persistence.ExecutionDurableRow`."""
        from bots5.desktop.control_bridge import ExecutionProjection, ExecutionState
        from bots5.core.queue_state_machine import ExecutionQueueState

        item = row.item
        receipt = item.receipt
        state_map = {
            ExecutionQueueState.PENDING: ExecutionState.PENDING,
            ExecutionQueueState.QUEUED: ExecutionState.QUEUED,
            ExecutionQueueState.RUNNING: ExecutionState.RUNNING,
            ExecutionQueueState.SETTLING: ExecutionState.RUNNING,
            ExecutionQueueState.COMPLETED: ExecutionState.SUCCEEDED,
            ExecutionQueueState.FAILED: ExecutionState.FAILED,
            ExecutionQueueState.CANCELLED: ExecutionState.CANCELLED,
            ExecutionQueueState.UNKNOWN: ExecutionState.UNKNOWN,
        }
        state = state_map[item.state]

        if receipt is not None:
            started_at = self._iso_to_timestamp(receipt.started_at)
            ended_at = self._iso_to_timestamp(receipt.ended_at)
            duration_seconds = receipt.duration_seconds
            error_type = receipt.error_type
            error_message = receipt.error_message
            provider_side_outcome_unknown = receipt.provider_side_outcome_unknown
        else:
            started_at = None
            ended_at = None
            duration_seconds = None
            error_type = None
            error_message = None
            provider_side_outcome_unknown = True

        return ExecutionProjection(
            operation_id=row.operation_id,
            kind=row.kind or item.operation_id or "",
            state=state.value,
            scope=row.scope,
            description=row.description or "",
            submitted_at=self._iso_to_timestamp(row.created_at),
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=duration_seconds,
            exit_code=None,
            error_type=error_type,
            error_message=error_message,
            provider_side_outcome_unknown=provider_side_outcome_unknown,
            grant_id=row.grant_id,
            approval_id=row.approval_id,
            output_path=None,
        )

    def _durable_row_to_receipt_projection(self, row) -> object:
        """Map one durable queue receipt to a :class:`ReceiptProjection`."""
        from bots5.desktop.control_bridge import ReceiptProjection, ReceiptState

        item = row.item
        receipt = item.receipt
        if receipt is None:
            return None

        provider_unknown = receipt.provider_side_outcome_unknown
        if item.state.value == "completed":
            receipt_state = ReceiptState.SETTLED
        elif item.state.value in ("failed", "cancelled") and not provider_unknown:
            receipt_state = ReceiptState.SETTLED
        elif item.state.value == "cancelled" and provider_unknown:
            receipt_state = ReceiptState.UNKNOWN
        else:
            receipt_state = ReceiptState.UNKNOWN

        return ReceiptProjection(
            receipt_id=f"queue-{row.operation_id}",
            operation_id=row.operation_id,
            state=receipt_state.value,
            settled_at=self._iso_to_timestamp(receipt.ended_at),
            request_digest=None,
            result_digest=None,
            grant_id=row.grant_id,
            unknown_reason=receipt.error_message if provider_unknown else None,
        )

    # -- reads consumed by ControlBridge.projection() --------------------

    def grants(self) -> tuple:
        from bots5.desktop.control_bridge import GrantProjection

        issued_by = f"capability-authority:{id(self._capability_authority):x}"
        projections: list[GrantProjection] = []
        for grant in self._capability_authority.inventory():
            subject = f"{grant.subject.kind}:{grant.subject.identity}"
            scope = grant.scope
            if isinstance(scope, DirectoryScope):
                subject = str(scope.root)
            projections.append(
                GrantProjection(
                    grant_id=grant.grant_id,
                    scope=grant.kind,
                    subject=subject,
                    issued_at=grant.issued_at.timestamp(),
                    expires_at=None,  # monotonic deadline; wall clock unknown
                    issued_by=issued_by,
                    request_digest=None,
                    binding_scope=None,  # not available from capability-authority grants
                    active=True,
                )
            )
        return tuple(projections)

    def executions(self) -> tuple:
        """Execution projections: durable store rows merged with in-memory mirror."""
        from bots5.desktop.control_bridge import ExecutionProjection

        if self._queue_store is None:
            return tuple(self._executions.values())

        projections: dict[str, ExecutionProjection] = {}
        for row in self._queue_store.load_durable_execution_state():
            proj = self._durable_row_to_execution_projection(row)
            projections[proj.operation_id] = proj

        # Merge observations that have not been written through to the store
        # (e.g. settle hooks that still add in-memory projections).
        for proj in self._executions.values():
            if isinstance(proj, ExecutionProjection) and proj.operation_id not in projections:
                projections[proj.operation_id] = proj

        return tuple(projections.values())

    def receipts(self) -> tuple:
        """Receipt projections: durable store rows merged with in-memory mirror."""
        from bots5.desktop.control_bridge import ReceiptProjection

        if self._queue_store is None:
            return tuple(self._receipts.values())

        projections: dict[str, ReceiptProjection] = {}
        for row in self._queue_store.load_durable_execution_state():
            proj = self._durable_row_to_receipt_projection(row)
            if proj is not None:
                projections[proj.receipt_id] = proj

        for proj in self._receipts.values():
            if isinstance(proj, ReceiptProjection) and proj.receipt_id not in projections:
                projections[proj.receipt_id] = proj

        return tuple(projections.values())

    def warnings(self) -> tuple:
        return tuple(self._warnings)

    # -- item views + mutation surface used by the bridge ----------------

    def grant_items(self) -> tuple:
        return self.grants()

    def execution_items(self) -> tuple:
        return self.executions()

    def receipt_items(self) -> tuple:
        return self.receipts()

    def add_grant(self, grant: object) -> None:
        # Grants are minted only through the shared CapabilityAuthority;
        # the bridge must never fabricate one through this surface.
        raise StateError("control plane grants are minted by the capability authority only")

    def add_execution(self, execution: object) -> None:
        self._executions[execution.operation_id] = execution

    def get_execution(self, operation_id: str):
        if self._queue_store is not None:
            for row in self._queue_store.load_durable_execution_state():
                if row.operation_id == operation_id:
                    return self._durable_row_to_execution_projection(row)
        return self._executions.get(operation_id)

    def cancel_execution(self, operation_id: str) -> None:
        """Transition a durable queue item to CANCELLED.

        No-op when no queue store is attached or the item is already terminal.
        """
        if self._queue_store is None:
            return
        from bots5.core.queue_state_machine import (
            ExecutionQueueState,
            transition,
        )

        item = self._queue_store.load_item(operation_id)
        if item is None or item.state not in (
            ExecutionQueueState.PENDING,
            ExecutionQueueState.QUEUED,
            ExecutionQueueState.RUNNING,
        ):
            return
        cancelled = transition(item, item.revision, ExecutionQueueState.CANCELLED)
        self._queue_store.save_item(cancelled)

    def add_receipt(self, receipt: object) -> None:
        self._receipts[receipt.receipt_id] = receipt

    def add_warning(self, warning: str) -> None:
        self._warnings.append(warning)

    # -- observation hooks (where real consumers report their effects) ---

    def observe_process_execution(self, receipt) -> None:
        """Project one settled ``ProcessReceipt`` into the shared views.

        The executor's own receipt is the evidence; state mapping is
        conservative — a remote/process outcome that cannot be established
        stays UNKNOWN and provider-side-unknown, never rewritten.
        """
        from bots5.desktop.control_bridge import (
            ExecutionProjection,
            ExecutionState,
            ReceiptProjection,
            ReceiptState,
        )
        from bots5.infrastructure.process_execution import ProcessState

        result = receipt.result
        if result.state is ProcessState.COMPLETED and result.exit_code == 0:
            state = ExecutionState.SUCCEEDED
            unknown = False
        elif result.remote_outcome_unknown or result.state is ProcessState.UNKNOWN:
            state = ExecutionState.UNKNOWN
            unknown = True
        elif result.state is ProcessState.CANCELLED:
            state = ExecutionState.CANCELLED
            unknown = True
        elif result.state is ProcessState.TIMEOUT:
            state = ExecutionState.TIMED_OUT
            unknown = False
        else:
            state = ExecutionState.FAILED
            unknown = False
        self.add_execution(
            ExecutionProjection(
                operation_id=result.process_id,
                kind="process",
                state=state.value,
                scope="process_execution",
                description=f"bounded process {result.process_id}",
                submitted_at=result.started_at,
                started_at=result.started_at,
                ended_at=result.ended_at,
                duration_seconds=None,
                exit_code=result.exit_code,
                error_type=result.error_message.split(":", 1)[0] if result.error_message else None,
                error_message=result.error_message,
                provider_side_outcome_unknown=unknown,
                grant_id=receipt.authority_grant_id,
                approval_id=None,
                output_path=None,
            )
        )
        self.add_receipt(
            ReceiptProjection(
                receipt_id=f"proc-{result.process_id}",
                operation_id=result.process_id,
                state=(
                    ReceiptState.SETTLED.value
                    if not result.remote_outcome_unknown
                    else ReceiptState.UNKNOWN.value
                ),
                settled_at=receipt.settled_at,
                request_digest=receipt.request_digest,
                result_digest=None,
                grant_id=receipt.authority_grant_id,
                unknown_reason=result.error_message if result.remote_outcome_unknown else None,
            )
        )

    def observe_git_operation(self, result, *, grant=None) -> None:
        """Project one settled Git operation through the shared views.

        ``grant`` is the seam :class:`CapabilityGrant` the caller held when
        authorizing the operation (the Git manager validates its OWN
        GitAuthorityLevel grant internally; BLK-01/03 owns that wiring).
        """
        from bots5.desktop.control_bridge import (
            ExecutionProjection,
            ExecutionState,
            ReceiptProjection,
            ReceiptState,
        )
        from bots5.infrastructure.git_authority import GitOperationState

        if result.state is GitOperationState.COMPLETED:
            state = ExecutionState.SUCCEEDED
            unknown = False
        elif result.state is GitOperationState.REJECTED:
            state = ExecutionState.FAILED
            unknown = False
        elif result.remote_outcome_unknown or result.state is GitOperationState.UNKNOWN:
            state = ExecutionState.UNKNOWN
            unknown = True
        else:
            state = ExecutionState.FAILED
            unknown = False
        self.add_execution(
            ExecutionProjection(
                operation_id=result.operation_id,
                kind="git",
                state=state.value,
                scope=(
                    "git_inspect"
                    if result.authority_level is GitAuthorityLevel.INSPECT
                    else "git_mutation"
                ),
                description=f"git {result.authority_level.value.lower()}",
                submitted_at=result.started_at,
                started_at=result.started_at,
                ended_at=result.ended_at,
                duration_seconds=None,
                exit_code=result.exit_code,
                error_type=None if result.error_message is None else "git_error",
                error_message=result.error_message,
                provider_side_outcome_unknown=unknown,
                grant_id=grant.grant_id if grant is not None else result.grant_id,
                approval_id=None,
                output_path=None,
            )
        )
        self.add_receipt(
            ReceiptProjection(
                receipt_id=f"git-{result.operation_id}",
                operation_id=result.operation_id,
                state=(
                    ReceiptState.SETTLED.value
                    if not unknown
                    else ReceiptState.UNKNOWN.value
                ),
                settled_at=result.ended_at if result.ended_at is not None else time.time(),
                request_digest=None,
                result_digest=None,
                grant_id=grant.grant_id if grant is not None else result.grant_id,
                unknown_reason=result.error_message if unknown else None,
            )
        )

    def observe_tool_invocation(self, result: object) -> None:
        """Project one settled ``ToolResult`` into the shared execution view.

        The tool invoker's journal is the evidence; this never invents a
        state the journal does not show.  A REFUSED invocation counts as a
        definite failure (zero-spend refusal); a SUCCEEDED one clears the
        provider-side uncertainty; UNKNOWN keeps it.
        """
        from bots5.core.tools import ToolState
        from bots5.desktop.control_bridge import ExecutionProjection, ExecutionState

        state_map = {
            ToolState.SUCCEEDED: ExecutionState.SUCCEEDED,
            ToolState.FAILED: ExecutionState.FAILED,
            ToolState.REFUSED: ExecutionState.FAILED,
            ToolState.UNKNOWN: ExecutionState.UNKNOWN,
        }
        mapped = state_map[result.state]
        now = time.time()
        provider_unknown = result.state in (ToolState.UNKNOWN,)
        error_type = None
        if result.state in (ToolState.FAILED, ToolState.UNKNOWN):
            error_type = "tool_error"
        elif result.state is ToolState.REFUSED:
            error_type = (
                result.refusal_reason.value if result.refusal_reason is not None else "refused"
            )
        self.add_execution(
            ExecutionProjection(
                operation_id=result.invocation_id,
                kind="tool",
                state=mapped.value,
                scope="tool_invocation",
                description=f"tool {result.tool_id}",
                submitted_at=now,
                started_at=now,
                ended_at=now,
                duration_seconds=None,
                exit_code=None,
                error_type=error_type,
                error_message=result.error,
                provider_side_outcome_unknown=provider_unknown,
                grant_id=None,
                approval_id=None,
                output_path=None,
            )
        )


@dataclass(slots=True)
class DesktopRuntime:
    paths: AppPaths
    authority: AuthorityLock
    application: BotsApplication
    session: DesktopSessionInfo
    workspace: DesktopSessionController
    # v0.2 BLK-04 shared composition (decision 9): ONE capability authority,
    # one control-plane state source, and the controlled egress consumer are
    # constructed by build_runtime and passed in here explicitly; every
    # consumer — the application's tool invoker and every window's control
    # bridge — receives these same instances, never a second copy.
    capability_authority: CapabilityAuthority = field(default=None)
    control_plane_state: "ControlPlaneState | None" = field(default=None)
    egress_consumer: ControlledEgressConsumer = field(default=None)
    # Plugin host wired to the shared capability authority (decision 9).
    # None on legacy test constructions.
    plugin_host: "PluginHost | None" = field(default=None)
    # Shared queue/receipt machinery composed by build_runtime (see the
    # close stages below); None on legacy test constructions.
    _git_manager: GitAuthorityManager | None = field(
        default=None, init=False, repr=False
    )
    _process_executor: BoundedProcessExecutor | None = field(
        default=None, init=False, repr=False
    )
    _code_executor: CodeExecutor | None = field(default=None, init=False, repr=False)
    windows: list[object] = field(default_factory=list)
    _opening_windows: set[asyncio.Task[None]] = field(default_factory=set)
    _close_state: ApplicationCloseState = field(
        default=ApplicationCloseState.OPEN, init=False
    )
    _close_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False)
    _close_task: asyncio.Task[TerminalCloseResult] | None = field(
        default=None, init=False
    )
    _close_result: TerminalCloseResult | None = field(default=None, init=False)
    # Slice E (workflow 8): the restore handoff is owned by the runtime — the
    # registry lives here and the capability is constructed with this runtime
    # instance in __post_init__ (i.e. by every DesktopRuntime(...) build_runtime
    # composition), never as a module-level singleton (F-05).
    _handoff_registry: RestoreHandoffRegistry = field(
        default=None, init=False, repr=False
    )
    handoff: RestoreHandoffCapability = field(default=None, init=False, repr=False)
    restore_exit_code: int | None = field(default=None, init=False, repr=False)
    # Phase 10: every CampaignBridge the runtime hands to a window is tracked
    # here so the bounded close stage can cancel and drain hosted campaign work
    # before the application and the data-root authority are released.
    _campaign_bridges: set[object] = field(
        default_factory=set, init=False, repr=False
    )
    # v0.2: every ControlBridge the runtime hands to a window is tracked
    # here so the bounded close stage can drain hosted control-plane work
    # before the application and the data-root authority are released.
    _control_bridges: set[object] = field(
        default_factory=set, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._handoff_registry = RestoreHandoffRegistry()
        self.handoff = RestoreHandoffCapability(self)

    # ------------------------------------------------------------------
    # v0.2 shared control plane (BLK-04)
    # ------------------------------------------------------------------

    @property
    def tool_invoker(self):
        """The application's ToolInvoker, built on the ONE shared authority.

        Returns ``None`` only for a runtime composed without the shared
        seam (legacy test constructions); every ``build_runtime`` desktop
        has one.
        """
        return getattr(self.application, "_tool_invoker", None)

    @property
    def git_authority_manager(self):
        """The shared GitAuthorityManager this runtime composed, or None."""
        return self._git_manager

    @property
    def process_executor(self):
        """The shared BoundedProcessExecutor this runtime composed, or None."""
        return self._process_executor

    @property
    def code_executor(self):
        """The shared CodeExecutor this runtime composed, or None."""
        return self._code_executor

    def _campaign_bridge_factory(self, runs_dir):
        """Compose and track a bridge for a window's campaign dock."""
        bridge = CampaignBridge(runs_dir)
        self._campaign_bridges.add(bridge)
        return bridge

    def _control_bridge_factory(self, state_dir):
        """Compose and track a bridge for a window's control plane dock.

        BLK-04: the bridge projects the runtime's shared control-plane state
        source (which reads live grants from the ONE capability authority)
        instead of private per-window dicts. When the shared source is used,
        the bridge also receives the shared authority so operator approval
        issues real, bounded grants. With no composed source the previous
        in-memory default is used unchanged.
        """
        if self.control_plane_state is not None:
            bridge = ControlBridge(
                state_dir=state_dir,
                state_source=self.control_plane_state,
                capability_authority=self.capability_authority,
            )
        else:
            bridge = ControlBridge(state_dir=state_dir)
        self._control_bridges.add(bridge)
        return bridge

    async def _close_campaigns(self) -> None:
        """Bounded Phase 10 close stage.

        Cancels and drains hosted campaign work, awaiting a durable terminal
        record.  It never retries provider work and never converts an uncertain
        outcome into success.  Runs before ``application.close()`` and before
        the data-root authority is released.
        """
        for bridge in tuple(self._campaign_bridges):
            try:
                if bridge.is_busy:
                    await asyncio.wait_for(
                        bridge.cancel(), _CAMPAIGN_CLOSE_TIMEOUT_SECONDS
                    )
                await asyncio.wait_for(
                    bridge.close(), _CAMPAIGN_CLOSE_TIMEOUT_SECONDS
                )
            finally:
                self._campaign_bridges.discard(bridge)

    async def _close_control_bridges(self) -> None:
        """Bounded v0.2 control plane close stage.

        Cancels and drains hosted control-plane work, awaiting a durable
        terminal record.  It never retries provider work and never converts
        an uncertain outcome into success.  Runs before ``application.close()``
        and before the data-root authority is released.
        """
        _CONTROL_CLOSE_TIMEOUT_SECONDS = 10
        for bridge in tuple(self._control_bridges):
            try:
                await asyncio.wait_for(
                    bridge.close_async(), _CONTROL_CLOSE_TIMEOUT_SECONDS
                )
            except Exception:
                try:
                    bridge.close()
                except Exception:
                    pass
            finally:
                self._control_bridges.discard(bridge)

    async def _close_control_machinery(self) -> None:
        """Bounded v0.2 close of the shared queue/receipt machinery.

        The composed process executor drains/cancels any hosted process
        before the application and the data-root authority are released.
        With no composed machinery (legacy runtime construction) this is a
        no-op, exactly as before BLK-04.
        """
        _CONTROL_CLOSE_TIMEOUT_SECONDS = 10
        executor = self._process_executor
        if executor is None:
            return
        await asyncio.wait_for(executor.shutdown(), _CONTROL_CLOSE_TIMEOUT_SECONDS)

    def _forget_window(self, window: object) -> None:
        if window in self.windows:
            self.windows.remove(window)

    def _request_new_window(self) -> None:
        if self._close_state is not ApplicationCloseState.OPEN:
            return
        task = asyncio.create_task(self.open_window())
        self._opening_windows.add(task)
        task.add_done_callback(self._opening_windows.discard)

    async def open_window(self, state=None):
        if self._close_state is not ApplicationCloseState.OPEN:
            raise StateError("desktop runtime is closed")
        from bots5.desktop.window import MainWindow

        window = MainWindow(
            self.application,
            self.session,
            workspace=self.workspace,
            window_state=state,
            # Slice E (workflow 8): the runtime-owned restore handoff is the
            # one injected route from the restore dialog to the close step.
            handoff=self.handoff,
            # Phase 10 M2.0b: campaign bridge factory for the campaign dock.
            # The runtime tracks every bridge it composes so the bounded close
            # stage below can drain hosted campaign work before authority
            # release.
            campaign_bridge_factory=self._campaign_bridge_factory,
            # v0.2: control bridge factory for the control plane dock.
            # The runtime tracks every bridge it composes so the bounded close
            # stage can drain hosted control-plane work before authority
            # release.
            control_bridge_factory=self._control_bridge_factory,
        )
        self.windows.append(window)
        window.closed.connect(lambda window=window: self._forget_window(window))
        window.new_window_requested.connect(
            lambda: self._request_new_window()
        )
        try:
            await window.initialize()
            window.show()
        except BaseException:
            self._forget_window(window)
            await window.stop_bridge_async()
            raise
        return window

    @staticmethod
    def _runtime_error(stage: str, *, authority: bool = False) -> TerminalCloseError:
        return TerminalCloseError(
            stage=stage,
            code=f"runtime_close_{stage}_failed",
            public_kind="authority" if authority else "state",
            message=f"desktop runtime close failed during {stage}",
        )

    async def _close_driver(self) -> TerminalCloseResult:
        errors: list[TerminalCloseError] = []
        try:
            for task in tuple(self._opening_windows):
                if not task.done():
                    task.cancel()
            if self._opening_windows:
                results = await asyncio.gather(
                    *self._opening_windows, return_exceptions=True
                )
                if any(
                    isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)
                    for result in results
                ):
                    errors.append(self._runtime_error("opening_windows"))
        except BaseException:
            errors.append(self._runtime_error("opening_windows"))

        try:
            await self.workspace.close()
        except BaseException:
            errors.append(self._runtime_error("workspace"))

        # Phase 10: bounded campaign close stage.  Hosted campaign work is
        # cancelled and drained to a durable terminal record BEFORE the
        # application closes and BEFORE the data-root authority is released.
        try:
            await self._close_campaigns()
        except BaseException:
            errors.append(self._runtime_error("campaign"))

        # v0.2: bounded control-plane close stage.  Hosted control-plane work
        # is drained BEFORE the application closes and BEFORE the data-root
        # authority is released.
        try:
            await self._close_control_bridges()
        except BaseException:
            errors.append(self._runtime_error("control"))

        # v0.2 BLK-04: the shared process/code/git machinery drains in the
        # same rank as the bridges — before application close and authority
        # release — so no composed consumer outlives the lease it acts under.
        try:
            await self._close_control_machinery()
        except BaseException:
            errors.append(self._runtime_error("control"))

        try:
            await self.application.close()
        except BaseException:
            application_result = self.application._close_result
            if application_result is None:
                errors.append(self._runtime_error("application", authority=True))
            else:
                errors.extend(application_result.errors)

        try:
            self.authority.release()
        except BaseException:
            errors.append(self._runtime_error("outer_authority", authority=True))

        # Phase 10 adds one key at the existing workspace rank; every
        # pre-existing stage keeps its original precedence.
        # v0.2: control at the same rank as campaign (both drain before
        # application close and authority release).
        precedence = {
            "store": 0,
            "outer_authority": 1,
            "application": 1,
            "workspace": 2,
            "campaign": 2,
            "control": 2,
            "opening_windows": 2,
            "execution": 3,
            "reconciliation": 4,
            "events": 5,
        }
        errors.sort(key=lambda error: precedence[error.stage])
        result = TerminalCloseResult(tuple(errors))
        self._close_result = result
        self._close_state = (
            ApplicationCloseState.CLOSED
            if result.succeeded
            else ApplicationCloseState.FAILED
        )
        return result

    def _forget_close_task(
        self, task: asyncio.Task[TerminalCloseResult]
    ) -> None:
        if self._close_task is task:
            task.result()
            self._close_task = None

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        if self._close_state is ApplicationCloseState.OPEN:
            self._close_state = ApplicationCloseState.CLOSING
            self._close_loop = loop
            task = loop.create_task(self._close_driver())
            self._close_task = task
            task.add_done_callback(self._forget_close_task)
        elif self._close_result is None and self._close_loop is not loop:
            raise StateError("desktop runtime close belongs to another event loop")
        result = self._close_result
        if result is None:
            task = self._close_task
            if task is None:
                raise StateError("desktop runtime close has no terminal operation")
            result = await asyncio.shield(task)
        if not result.succeeded:
            error = result.errors[0]
            if error.public_kind == "authority":
                raise AuthorityError(error.message) from None
            raise StateError(error.message) from None


class RestoreStartupCoordinator:
    """Slice D startup interception (invariant I12).

    Runs in ``build_runtime`` after ``paths.ensure_non_authoritative()`` and
    before ``authority.open_store()`` so that an interrupted whole-install
    restore is reconciled strictly from its durable journal before normal
    startup can fabricate or open a fresh installation.  With no restore
    journal present the coordinator performs no live mutation and normal
    startup is unchanged in behaviour.

    The coordinator owns the two remaining coordinator-side obligations:

    - receipt finalisation: on a later startup, once the adopted
      installation has reached the application head, the restore receipt is
      updated to ``post_adoption_migration="completed"`` with
      ``target_revision_after_migration``.  If the deferred post-adoption
      forward migration failed and rolled back (D-B=B.1), the durable
      migration recovery journal attributes that failure and the receipt
      records ``post_adoption_migration="failed_rolled_back"`` together
      with the migration recovery reference; evidence that cannot be
      attributed raises fail-closed and never fabricates a terminal state.
      This is a receipt-record update only — no authoritative data
      mutation, no lifecycle state, no reopening of the restore journal.
    - the D-D=D.2 destructive-override surface: an explicit, separately
      supplied operator authorization (keyword-only on ``build_runtime``,
      default OFF, no UI).  With the authorization supplied, a pending
      restore transaction halted at ``PRESERVING`` — the one state in which
      no rollback source exists — is *executed* destructively by the M5
      ``RestoreService.execute_destructive_override`` machine, which
      re-proves all four validity conditions under the transaction lock and
      never waives authority/target-identity uncertainty.  A supplied
      authorization that cannot be exercised (no qualifying transaction, a
      rollback-capable transaction, an unverifiable source) stops startup
      fail-closed instead of being silently discarded; the normal restore
      path never falls back to the override.
    """

    def __init__(
        self,
        authority: AuthorityLock,
        *,
        destructive_override: bool = False,
    ) -> None:
        if type(destructive_override) is not bool:
            raise TypeError(
                "destructive_override must be an explicit bool operator "
                "authorization; it is never implied or defaulted"
            )
        self._authority = authority
        self._destructive_override = destructive_override

    @property
    def destructive_override_authorized(self) -> bool:
        """The separately-supplied operator authorization (default OFF)."""
        return self._destructive_override

    def before_store_open(self) -> dict[str, object] | None:
        """Reconcile any interrupted restore, then finalise its receipt.

        With the D-D=D.2 operator authorization supplied, a transaction
        halted at ``PRESERVING`` is executed destructively instead of being
        aborted; a supplied authorization that cannot be exercised raises
        and stops startup (fail closed, never silently discarded).
        Otherwise the interrupted restore is reconciled strictly from its
        durable journal.  Either way an unattributable or ambiguous journal
        state raises and stops startup, and a rolled-back restore requires a
        clean restart.  Returns the reconciliation/finalisation summary when
        a journal or a pending receipt was observed, else ``None``.
        """
        service = RestoreService(self._authority)
        summary: dict[str, object] | None = None
        if self._destructive_override:
            summary = self.execute_destructive_override()
        else:
            summary = service.reconcile()
        if summary is not None:
            action = summary.get("action")
            if action not in {"aborted", "completed", "awaiting-restart"}:
                raise BackupError(
                    "restore reconciliation returned an unattributable action: "
                    f"{action!r}"
                )
            if summary.get("restart_required"):
                raise BackupError(
                    "restore rolled back to the preserved installation; restart "
                    "required before startup can continue"
                )
        finalisation = service.finalise_restore_receipt()
        if finalisation is not None:
            action = finalisation.get("action")
            if action not in {
                "pending_migration",
                "migration_recovery_pending",
                "finalised",
                "failed_rolled_back",
            }:
                raise BackupError(
                    "restore receipt finalisation returned an unattributable "
                    f"action: {action!r}"
                )
        if summary is None and finalisation is None:
            return None
        return {"reconciliation": summary, "receipt_finalisation": finalisation}

    def evaluate_destructive_override(self) -> dict[str, object]:
        """Evaluate the D-D=D.2 gate with the build_runtime operator authorization.

        Delegates to the restore service's gate; refuses unless the target
        identity is proven, the Backup v1 source independently verified, the
        current installation truthfully will not remain rollback-capable,
        and the operator explicitly authorized the destructive consequence.
        """
        return RestoreService(self._authority).evaluate_destructive_override(
            operator_authorized=self._destructive_override
        )

    def execute_destructive_override(self) -> dict[str, object]:
        """Execute the D-D=D.2 destructive continuation (M5).

        Runs only when the separately supplied operator authorization is
        set; the restore service re-proves all four validity conditions
        under the transaction lock before the destructive step and fails
        closed on any authority/target-identity uncertainty.  A supplied
        authorization that cannot be exercised raises and stops startup
        instead of being silently discarded.
        """
        return RestoreService(self._authority).execute_destructive_override(
            operator_authorized=self._destructive_override
        )


def _prepare_data_root_topology(paths: AppPaths) -> None:
    """Prepare the data-root topology every startup entry point shares.

    The XDG application directories live under the data root whenever an
    explicit override root is used, so creating them changes the root
    topology.  They must exist before the authority baselines the root:
    every acquisition — and therefore every restore journal identity
    record — must observe the same root topology for restart
    reconciliation to attribute a journal by identity (I11) instead of
    failing closed forever on a transient first-run link-count delta.

    The data root itself must therefore exist first, and it must be
    owner-only: DataRootAuthority._check_directory requires mode 0o700 and
    creates the root that way when it does not exist.  Creating the XDG
    directories first would otherwise let mkdir() create the root with
    default (umask-derived) permissions, so a genuine first-ever run would
    fail acquisition with "unsafe owner or permissions".
    """
    paths.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.ensure_non_authoritative()


def build_runtime(
    data_root: Path | None = None,
    *,
    backend: str = "fake",
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str | None = None,
    reasoning_effort: ReasoningEffort | None = None,
    destructive_restore_override: bool = False,
    developer_provider_test_mode: bool = False,
    workspace_root: Path | None = None,
    egress_transport=None,
) -> DesktopRuntime:
    if type(developer_provider_test_mode) is not bool:
        raise ValueError("developer provider test mode requires an explicit boolean")
    if developer_provider_test_mode and backend != "fake":
        raise ValueError("developer provider test mode requires the configured-provider desktop")
    from bots5.desktop.session import DesktopSessionController

    paths = resolve_app_paths(data_root)
    _prepare_data_root_topology(paths)
    authority = AuthorityLock(paths.data_root).acquire()
    try:
        # Slice D (I12): restore interception.  If a restore journal is
        # present, reconcile the interrupted restore before normal startup
        # can fabricate or open a fresh installation; with no journal this
        # changes nothing.  Must stay after the root is baselined and
        # non-authoritative state is prepared, and before open_store().
        RestoreStartupCoordinator(
            authority,
            destructive_override=destructive_restore_override,
        ).before_store_open()
        store = authority.open_store()
        clock = SystemClock()
        ids = Uuid7Factory()
        events = EventBus(clock, ids)
        if backend == "fake":
            generation_backend = BuiltinProviderRouter(fake_backend=FakeStreamingBackend())
            backend_id = "fake"
            selected_model = "fake-v0.1"
            provider_id = None
            selected_base_url = None
            selected_api_key_env = None
        elif backend == "local_openai":
            if not base_url or not model:
                raise ValueError(
                    "local_openai requires --base-url and --model"
                )
            provider = OpenAICompatibleProvider(base_url, api_key_env=api_key_env)
            generation_backend = OpenAICompatibleStreamingBackend(
                provider,
                provider_id="local_openai",
                base_url=provider.base_url,
                api_key_env=provider.api_key_env,
                reasoning_effort=reasoning_effort,
            )
            backend_id = OpenAICompatibleStreamingBackend.backend_id
            selected_model = model
            provider_id = "local_openai"
            selected_base_url = provider.base_url
            selected_api_key_env = provider.api_key_env
        else:
            raise ValueError(f"unsupported desktop backend: {backend}")
        configuration = (
            ProviderConfiguration(
                store, ids, clock, secret_store_factory=secret_store_for,
                phase6_enabled=not developer_provider_test_mode,
            )
            if backend == "fake"
            else None
        )
        generation_mode = (
            GenerationMode.CONFIGURED
            if backend == "fake"
            else GenerationMode.LEGACY_PHASE3_LOCAL_OPENAI
        )
        backup_service = BackupService(
            RootedBackupCaptureAdapter(
                authority,
                store,
                paths,
                BackupZipPackageAdapter(),
                data_root_is_override=data_root is not None,
            ),
            BackupZipPackageAdapter(),
            BackupFilePublicationAdapter(),
            ids,
        )
        # v0.2 BLK-04 shared composition (decision 9 + decision 7).
        # ONE CapabilityAuthority instance is constructed here at the
        # application root and injected into every v0.2 consumer: the
        # application's tool invoker, the control bridge's grant projection
        # (through ControlPlaneState), and the controlled egress consumer.
        # Seam grants themselves stay process-local by design — that is the
        # seam's documented rule; what IS durable here is the queue/receipt
        # store attached below (BLK-06 machinery).
        capability_authority = CapabilityAuthority(clock=clock)
        # Shared queue/receipt machinery: one execution manager, one bounded
        # process executor (its public execute() is grant-gated by BLK-02),
        # one code executor on top of it, one git authority manager sharing
        # the same executor, and one durable QueuePersistenceStore in the
        # classified data-root database directory bound to one
        # OwnedExecutionWorkers registry.
        execution_manager = ExecutionManager()
        queue_store_path = paths.data_root / "database" / "execution-queue.db"
        process_executor = BoundedProcessExecutor(
            execution_manager=execution_manager, clock=clock, ids=ids
        )
        code_executor = CodeExecutor(process_executor=process_executor, clock=clock, ids=ids)
        git_manager = GitAuthorityManager(
            process_executor=process_executor,
            execution_manager=execution_manager,
            clock=clock,
            ids=ids,
        )
        control_plane_state = ControlPlaneState(
            capability_authority=capability_authority,
            queue_store_path=queue_store_path,
        )
        # Decision 7: exactly ONE controlled egress consumer, bound to the
        # same authority.  It performs no real network I/O by default; the
        # transport is injectable (composition passes None unless given).
        egress_consumer = ControlledEgressConsumer(
            authority=capability_authority,
            transport=egress_transport,
        )
        # Plugin host wired to the shared capability authority (decision 9).
        # Plugins now use the same authority instance as tools and code execution.
        plugin_host = PluginHost(capability_authority=capability_authority)
        resolved_workspace_root = (
            workspace_root if workspace_root is not None else paths.data_root / "workspace"
        )
        application = BotsApplication(
            store,
            events,
            generation_backend,
            ids=ids,
            clock=clock,
            backend_id=backend_id,
            model=selected_model,
            provider_id=provider_id,
            base_url=selected_base_url,
            api_key_env=selected_api_key_env,
            configuration=configuration,
            generation_mode=generation_mode,
            developer_provider_test_mode=developer_provider_test_mode,
            backup_service=backup_service,
            capability_authority=capability_authority,
            workspace_root=resolved_workspace_root,
        )
        session = DesktopSessionInfo(
            backend_id=backend_id,
            model=selected_model,
            provider_id=provider_id,
            generation_mode=generation_mode.value,
            phase6_enabled=application.phase6_enabled,
            developer_provider_test_mode=application.developer_provider_test_mode,
        )
        runtime = DesktopRuntime(
            paths,
            authority,
            application,
            session,
            DesktopSessionController(application, session, ids=ids),
            capability_authority=capability_authority,
            control_plane_state=control_plane_state,
            egress_consumer=egress_consumer,
            plugin_host=plugin_host,
        )
        # The composed machinery is owned by the runtime (init=False fields):
        # one process executor / code executor / git authority manager built
        # on the shared clock, ids and execution discipline, drained by the
        # bounded close stage before application close and authority release.
        runtime._process_executor = process_executor
        runtime._code_executor = code_executor
        runtime._git_manager = git_manager
        return runtime
    except Exception:
        authority.release()
        raise


def _initiate_restore(
    package: Path,
    data_root: Path | None,
    expected_backup_id: str | None,
) -> int:
    """Authorized non-UI whole-installation restore initiation (Slice D).

    Enters at the accepted bootstrap/composition boundary: exactly the root
    preparation and ``DataRootAuthority`` acquisition of ``build_runtime``,
    then the accepted ``RestoreStartupCoordinator`` interception
    (``before_store_open``: reconcile any interrupted restore strictly
    before anything can open a store), then one ``RestoreService.restore``
    transaction — the existing restore coordinator/service, with every
    validation, preservation, staging, adoption, restart, rollback and
    fail-closed semantic intact.  Nothing on this path ever opens the
    store: the adopted installation is migrated forward and its receipt
    finalised by a later normal startup (D-B=B.1).  The destructive
    override stays at the coordinator default (OFF); this surface exposes
    no override of its own.

    It never imports Qt: the module import is Qt-free and the Qt machinery
    in ``main`` sits strictly behind the desktop branch.

    Typed outcomes are reported truthfully and the exit status follows
    them:

    - ``0`` — the restore committed: the receipt is written to stdout as
      canonical JSON, byte-identical to the durable receipt file.
    - ``2`` — not committed, typed refusal or typed rollback
      (``restore not committed: <qualified type>: <message>`` on stderr),
      e.g. package verification refusal, unsupported revision, quiescence
      or lock refusal, or ``restore rolled back to the preserved
      installation; restart required``.  A refused verification mutates no
      live state; a rollback converges the live installation back to the
      preserved pre-restore state and leaves the durable journal for the
      restart acknowledgment.
    - ``3`` — failed closed (``restore failed closed: ...`` on stderr):
      unattributable evidence, authority poisoned, human inspection
      required.
    - ``1`` — the restore committed but the data-root authority then failed
      to release cleanly, or an unexpected non-B.O.T.S. error escaped
      (fail loud).
    """
    paths = resolve_app_paths(data_root)
    _prepare_data_root_topology(paths)
    try:
        authority = AuthorityLock(paths.data_root).acquire()
    except CoreError as exc:
        print(
            f"restore not committed: {type(exc).__module__}.{type(exc).__qualname__}: {exc}",
            file=sys.stderr,
        )
        return 2
    try:
        RestoreStartupCoordinator(authority).before_store_open()
        receipt = RestoreService(authority).restore(
            package, expected_backup_id=expected_backup_id
        )
    except BackupUnclassifiedState as exc:
        outcome, code = (
            f"restore failed closed: "
            f"{type(exc).__module__}.{type(exc).__qualname__}: {exc}",
            3,
        )
    except CoreError as exc:
        outcome, code = (
            f"restore not committed: "
            f"{type(exc).__module__}.{type(exc).__qualname__}: {exc}",
            2,
        )
    else:
        sys.stdout.buffer.write(canonical_backup_json(receipt))
        sys.stdout.buffer.flush()
        return _release_after_restore_initiation(authority, 0)
    print(outcome, file=sys.stderr)
    return _release_after_restore_initiation(authority, code)


async def _create_restore_child(argv: list[str]) -> asyncio.subprocess.Process:
    """Spawn the waited pre-store bootstrap child with CAPTURED streams (S7).

    ``asyncio.create_subprocess_exec`` with piped stdout/stderr plus
    ``await proc.communicate()`` drains both pipes concurrently, so a chatty
    child can never fill a pipe and deadlock, and the raw canonical receipt
    (stdout) or typed refusal (stderr) is captured for the GUI-visible result
    surface instead of relying on an inherited terminal (F-06).
    """

    return await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _present_restore_handoff_result(
    runtime: "DesktopRuntime",
    *,
    argv: list[str],
    status: int,
    stdout_text: str,
    stderr_text: str,
) -> None:
    """S8: show the GUI-visible post-close result and await its dismissal.

    The qasync loop is still alive inside ``serve()``, so a parentless dialog
    can be presented even though every window is closed.  The dialog shows
    the raw canonical receipt or the typed refusal together with the EXACT
    0/1/2/3 exit status — the outcome is never collapsed into success/failure
    and never inferred from an optional terminal (F-06, invariant I6).
    """

    from bots5.desktop.phase9_dialogs import RestoreHandoffResultDialog

    dialog = RestoreHandoffResultDialog(
        data_root=runtime.paths.data_root,
        argv=argv,
        status=status,
        receipt_text=stdout_text,
        refusal_text=stderr_text,
    )
    loop = asyncio.get_running_loop()
    dismissed: asyncio.Future[None] = loop.create_future()

    def _on_finished(_result: int, future: asyncio.Future[None] = dismissed) -> None:
        if not future.done():
            future.set_result(None)

    dialog.finished.connect(_on_finished)
    dialog.show()
    try:
        await dismissed
    finally:
        try:
            dialog.finished.disconnect(_on_finished)
        except (RuntimeError, TypeError):
            pass


def _restore_bootstrap_command() -> list[str]:
    """Re-enter the desktop bootstrap using this runtime's execution model.

    Nuitka's executable already calls ``main`` before desktop/store
    composition. Its ``sys.executable`` can name a nonexistent bundled
    Python, so the Linux process image is the re-entry target. Source
    execution still needs the interpreter's module dispatch.
    """
    if "__compiled__" in globals():
        return [os.readlink("/proc/self/exe")]
    return [sys.executable, "-m", "bots5.bootstrap.desktop"]


async def _run_restore_handoff_post_close(runtime: "DesktopRuntime") -> None:
    """S7/S8: consume the one-shot request and run the waited bootstrap child.

    Called from ``serve``'s ``finally`` strictly after ``runtime.close()``
    has returned successfully and on the non-cancelled close path, while the
    qasync loop is still alive because ``serve()`` has not returned.  The
    invariant I2 ordering is structural: ``_close_driver`` releases the data
    root authority LAST (desktop.py ``_close_driver``), so the child below
    can only acquire a fresh authority after the live session has fully
    closed and released everything.  The child reaches the ``--restore-from``
    branch BEFORE any Qt import and runs the landed Slice D restore via
    ``_initiate_restore`` — this parent process never restores in place.

    The child runs WAITED with captured stdout/stderr; the exact child exit
    code is stored on ``runtime.restore_exit_code`` for ``main()`` to return,
    and the raw receipt/typed refusal plus the exact 0/1/2/3 status are
    presented in a GUI-visible dialog before ``serve()`` returns.

    Oracle R-7: the cancelled re-raise branch in ``serve`` re-raises before
    reaching this step, so a cancelled close never launches the child.

    Oracle R-8: a request whose close failed never reaches this step (the
    close failure raises out of ``serve``) and is NOT tidily cleared — it is
    abandoned by process termination, with no child launched.  Nothing here
    implies a tidy drop: the pending request simply dies with the process.
    """

    handoff = runtime.handoff
    if handoff is None:  # defensive; DesktopRuntime always builds one
        return
    request = handoff.take()
    if request is None:
        return
    argv = [
        *_restore_bootstrap_command(),
        "--data-root",
        os.fspath(runtime.paths.data_root),
        "--restore-from",
        os.fspath(request.package),
        "--expected-backup-id",
        request.expected_backup_id,
    ]
    process = await _create_restore_child(argv)
    stdout, stderr = await process.communicate()
    runtime.restore_exit_code = process.returncode
    await _present_restore_handoff_result(
        runtime,
        argv=argv,
        status=process.returncode,
        stdout_text=stdout.decode("utf-8", errors="replace"),
        stderr_text=stderr.decode("utf-8", errors="replace"),
    )


async def serve(runtime: "DesktopRuntime") -> None:
    """Run the composed desktop session, close the runtime, then (Slice E)
    consume an accepted restore handoff after the close succeeded.

    Landed behaviour is unchanged; the only addition is the inert-by-default
    post-close step at the end of the ``finally`` block.
    """

    workspace = runtime.workspace
    # build_runtime runs before qasync enters this loop.  Resume any
    # durable queue work before ordinary desktop admission without
    # exposing a UI lifecycle control surface.
    runtime.application._ensure_import_scheduler()
    states = tuple(
        state for state in await workspace.load_workspace() if state.restore_open
    )
    if not states:
        states = (None,)
    try:
        for state in states:
            await runtime.open_window(state)
        await workspace.wait_closed()
    finally:
        cancelled = False
        try:
            await runtime.close()
        except asyncio.CancelledError:
            cancelled = True
            # The outer waiter may be cancelled, but qasync must not
            # stop while the shared non-exceptional close driver owns
            # live application or authority capabilities.
            await runtime.close()
        if cancelled:
            # Oracle R-7: the cancelled re-raise branch must skip the
            # post-close restore child entirely.
            raise asyncio.CancelledError from None
        # Slice E (workflow 8): runs only after a successful, non-cancelled
        # close — the authority lease was released LAST inside
        # runtime.close(), so the pre-store bootstrap child can only acquire
        # a fresh authority afterwards.  A close failure raises above and
        # abandons any pending request to process termination (R-8): no
        # clear is pretended and no child is launched.
        await _run_restore_handoff_post_close(runtime)


def _release_after_restore_initiation(authority: AuthorityLock, code: int) -> int:
    """Release the authority after restore initiation without masking it.

    On a committed restore a release failure is integrity-relevant and
    turns the exit non-zero.  On a refused or failed-closed outcome the
    authority may already be poisoned; a terminal close failure then
    carries the same evidence that was already reported, so it is noted
    and never allowed to mask the restore outcome.
    """
    try:
        authority.release()
    except BaseException as exc:
        if code == 0:
            print(
                "restore committed but the data-root authority did not "
                f"release cleanly: {type(exc).__module__}.{type(exc).__qualname__}: {exc}",
                file=sys.stderr,
            )
            return 1
        print(
            "note: the data-root authority also failed to release cleanly: "
            f"{type(exc).__module__}.{type(exc).__qualname__}: {exc}",
            file=sys.stderr,
        )
    return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bots5-desktop")
    parser.add_argument(
        "--developer-provider-test-mode",
        action="store_true",
        help=(
            "TEMPORARY developer-only INEXACT / PROVIDER TEST MODE; disables "
            "Phase 6 planning for this session only. No deterministic context "
            "budgeting, history, or attachment guarantees. Not Phase 6 compliant."
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="override the XDG application data root",
    )
    parser.add_argument(
        "--backend",
        choices=("fake", "local_openai"),
        default="fake",
        help=(
            "generation backend; local_openai is explicit Phase 3 legacy "
            "compatibility mode (Phase 6 planning/accounting disabled)"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="normalized local OpenAI-compatible API base URL",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="model identifier for the selected real backend",
    )
    parser.add_argument(
        "--api-key-env",
        default=None,
        help="optional environment-variable name for local backend authentication",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("none",),
        default=None,
        help="optional OpenAI-compatible reasoning setting",
    )
    parser.add_argument(
        "--restore-from",
        type=Path,
        default=None,
        help=(
            "initiate one whole-installation restore from this Backup v1 "
            "package, report the typed outcome, and exit without opening "
            "the store or any UI (generation-backend options are not "
            "consulted on this path)"
        ),
    )
    parser.add_argument(
        "--expected-backup-id",
        default=None,
        help=(
            "with --restore-from: refuse the restore unless the package "
            "independently verifies against this backup id"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.developer_provider_test_mode and (
        args.backend != "fake" or args.restore_from is not None
    ):
        parser.error("--developer-provider-test-mode requires the configured-provider desktop, not local_openai or restore")
    if args.expected_backup_id is not None and args.restore_from is None:
        parser.error("--expected-backup-id requires --restore-from")
    if args.restore_from is not None:
        # Non-UI restore initiation: returns before any Qt import below.
        return _initiate_restore(
            args.restore_from, args.data_root, args.expected_backup_id
        )
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication
    from qasync import QEventLoop

    from bots5.desktop.window import MainWindow
    from bots5.desktop.application_icon import (
        APPLICATION_NAME, DESKTOP_FILE_NAME, application_icon,
    )

    QCoreApplication.setApplicationName(APPLICATION_NAME)
    qt_application = QApplication.instance() or QApplication(sys.argv)
    qt_application.setDesktopFileName(DESKTOP_FILE_NAME)
    qt_application.setWindowIcon(application_icon())
    original_quit_on_last_window_closed = qt_application.quitOnLastWindowClosed()
    qt_application.setQuitOnLastWindowClosed(False)

    try:
        event_loop = QEventLoop(qt_application)
        asyncio.set_event_loop(event_loop)
        try:
            runtime = build_runtime(
                args.data_root,
                backend=args.backend,
                base_url=args.base_url,
                model=args.model,
                api_key_env=args.api_key_env,
                reasoning_effort=args.reasoning_effort,
                developer_provider_test_mode=args.developer_provider_test_mode,
            )
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        try:
            with event_loop:
                event_loop.run_until_complete(serve(runtime))
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        # Slice E (workflow 8): when the post-close step ran, the child's
        # exact 0/1/2/3 exit code is the process exit code — never collapsed
        # into success/failure.  Without a handoff the existing 0 stands.
        if runtime.restore_exit_code is not None:
            return runtime.restore_exit_code
        return 0
    finally:
        qt_application.setQuitOnLastWindowClosed(original_quit_on_last_window_closed)


if __name__ == "__main__":
    raise SystemExit(main())
