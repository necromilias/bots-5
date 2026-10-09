"""Qt-free v0.2 control-plane bridge: the single desktop/control-plane seam.

This module is the ONLY seam through which the desktop observes or operates
the v0.2 control plane (authority grants, execution queue, approvals,
receipts). It is deliberately Qt-free: no PySide6, no qasync, no desktop
imports — directly unit-testable with plain ``asyncio``.

Authority discipline (non-negotiable):
- The core stays the only producer of control-plane evidence. This bridge
  never writes control-plane evidence itself except through the explicit
  command methods, which go through the core's own storage/execution helpers.
- Zero-spend work (inspect/list/prepare) constructs no executor, contacts no
  provider and writes nothing, so an abandoned approval leaves zero bytes.
- Executors are constructed in exactly one place (``approve_*``) and only
  after approval, through an injectable factory.
- The projection (``ControlProjection`` / ``project_control_state``) is built
  ONLY from durable filesystem/in-memory state and obeys the desktop-must-NEVER
  rules: live state is known + explicit unknown set — never fabricated; a
  durable ``running`` operation is provider-side-outcome-unknown; a
  post-dispatch failure is unknown unless the durable record proves a
  definitive rejection; nothing here ever auto-retries.
- Polling is bounded and read-only: ``POLL_INTERVAL_MS``/``POLL_MAX_INTERVAL_MS``
  match the existing campaign/import-queue discipline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from datetime import datetime, UTC
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..errors import ApprovalInvalidatedError, ValidationError
from ..core.capabilities import (
    CapabilityAuthority,
    CapabilityDenied,
    CapabilitySystemError,
    DirectoryScope,
    EgressDestination,
    EgressScope,
    GrantRequest,
    Subject,
)
from ..core.errors import StateError
from ..core.execution import ExecutionManager

__all__ = [
    "ControlBridge",
    "ControlProjection",
    "InMemoryControlState",
    "PreparedControlOperation",
    "GrantProjection",
    "ExecutionProjection",
    "ReceiptProjection",
    "POLL_INTERVAL_MS",
    "POLL_MAX_INTERVAL_MS",
    "project_control_state",
]

# Bounded polling contract: the dock owns the timer; the bridge carries the
# same timings so every poller using this seam is bounded identically.
POLL_INTERVAL_MS = 250
POLL_MAX_INTERVAL_MS = 1000

# D-9 reader rule: durable error types that PROVE a known provider-side
# outcome despite dispatch. Types absent from this set fail-safely to unknown.
_DEFINTIVE_DURABLE_ERROR_TYPES = frozenset({
    "ProviderResponseError",
    "ProcessExitKnown",
})


class GrantScope(str, Enum):
    """Scope of an authority grant."""
    GIT_INSPECT = "git_inspect"
    GIT_EDIT = "git_edit"
    GIT_STAGE = "git_stage"
    GIT_COMMIT = "git_commit"
    GIT_PUSH = "git_push"
    PROCESS_EXECUTION = "process_execution"
    TOOL_INVOCATION = "tool_invocation"
    NETWORK_EGRESS = "network_egress"
    PLUGIN_LOAD = "plugin_load"


class ExecutionState(str, Enum):
    """State of a control-plane execution."""
    PENDING = "pending"
    QUEUED = "queued"
    APPROVED = "approved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"


class ReceiptState(str, Enum):
    """Settlement state of a receipt."""
    SETTLED = "settled"
    UNKNOWN = "unknown"
    REJECTED = "rejected"


_TERMINAL_EXECUTION_STATES = frozenset({
    ExecutionState.SUCCEEDED,
    ExecutionState.FAILED,
    ExecutionState.CANCELLED,
    ExecutionState.TIMED_OUT,
    ExecutionState.UNKNOWN,
})


@dataclass(frozen=True, slots=True)
class GrantProjection:
    """What the UI may render about one authority grant.

    Built only from durable state; ``None`` means the durable record
    does not say.
    
    Binding scope:
    - LEVEL_SCOPE: grant is valid for any operation at this level/scope
    - EXACT_REQUEST: grant is valid only for the exact command that was authorized
    """
    grant_id: str
    scope: str  # GrantScope value
    subject: str  # What the grant applies to (repo path, tool name, etc.)
    issued_at: float
    expires_at: float | None
    issued_by: str
    request_digest: str | None
    active: bool
    revoked_at: float | None = None
    revoke_reason: str | None = None
    binding_scope: str | None = None  # "level_scope" or "exact_request"


@dataclass(frozen=True, slots=True)
class ExecutionProjection:
    """What the UI may render about one execution operation.

    Built only from durable state; ``None`` means the durable record
    does not say.
    """
    operation_id: str
    kind: str  # e.g., "git", "process", "tool", "plugin"
    state: str  # ExecutionState value
    scope: str | None  # GrantScope value if applicable
    description: str
    submitted_at: float
    started_at: float | None
    ended_at: float | None
    duration_seconds: float | None
    exit_code: int | None
    error_type: str | None
    error_message: str | None
    provider_side_outcome_unknown: bool
    grant_id: str | None
    approval_id: str | None
    output_path: str | None


@dataclass(frozen=True, slots=True)
class ReceiptProjection:
    """What the UI may render about one settlement receipt.

    Built only from durable state; ``None`` means the durable record
    does not say.
    """
    receipt_id: str
    operation_id: str
    state: str  # ReceiptState value
    settled_at: float | None
    request_digest: str | None
    result_digest: str | None
    grant_id: str | None
    unknown_reason: str | None


@dataclass(frozen=True)
class ControlProjection:
    """Immutable sealed projection of v0.2 control-plane state.

    Everything is optional/None when the durable record does not say;
    nothing is interpolated.
    """
    projection_time: float
    grants: tuple[GrantProjection, ...] = ()
    executions: tuple[ExecutionProjection, ...] = ()
    receipts: tuple[ReceiptProjection, ...] = ()
    active_count: int = 0
    queued_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    unknown_count: int = 0
    integrity_warnings: tuple[str, ...] = ()

    @property
    def has_active(self) -> bool:
        return self.active_count > 0

    @property
    def has_queued(self) -> bool:
        return self.queued_count > 0

    @property
    def display_state(self) -> str:
        if self.active_count > 0:
            return "running"
        if self.queued_count > 0:
            return "queued"
        if self.unknown_count > 0:
            return "uncertain"
        if self.failed_count > 0 and self.succeeded_count == 0:
            return "failed"
        if self.succeeded_count > 0:
            return "succeeded"
        return "idle"

    @property
    def is_running(self) -> bool:
        return self.active_count > 0


@dataclass(frozen=True)
class ApprovalRecord:
    """A one-shot approval record for a control-plane operation."""
    approval_id: str
    operation_id: str
    scope: str
    approved_by: str
    approved_at: float
    request_digest: str
    pricing_evidence: dict[str, Any] | None = None
    consumed: bool = False


@dataclass(frozen=True)
class PreparedControlOperation:
    """A zero-spend prepared operation awaiting approval.

    Constructed during ``prepare_*``; no executor is built, no provider
    is contacted, no bytes are written. The ``request_digest`` binds the
    approval to exactly this request shape.
    """
    operation_id: str
    kind: str
    scope: str
    description: str
    summary: str
    request_digest: str
    approval: ApprovalRecord
    grant_scope: str | None = None
    grant_scope_target: Path | EgressDestination | DirectoryScope | EgressScope | None = None
    operation: str = "execute"  # "execute" | "cancel" | "revoke_grant"
    estimated_resources: dict[str, Any] = field(default_factory=dict)


# --- zero-spend preflight helpers ------------------------------------------------

def _new_operation_id() -> str:
    """A fresh filesystem-safe operation id."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"op-{stamp}-{uuid4().hex[:8]}"


def _new_approval_id() -> str:
    """A fresh one-shot approval id."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"approval-{stamp}-{uuid4().hex[:8]}"


def _request_digest(data: dict[str, Any]) -> str:
    """Stable digest of a request shape, for approval binding."""
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _serialize_scope_target(target: Any) -> Any:
    """Serialize a grant scope target for request-digest binding.

    Normalizes concrete scope objects into JSON-safe primitives so the
    same logical target always produces the same digest, without issuing
    or consuming any grant.
    """
    if target is None:
        return None
    if isinstance(target, DirectoryScope):
        return str(target.root)
    if isinstance(target, EgressScope):
        return [
            {"scheme": d.scheme, "host": d.host, "port": d.port}
            for d in target.destinations
        ]
    if isinstance(target, EgressDestination):
        return {"scheme": target.scheme, "host": target.host, "port": target.port}
    if isinstance(target, Path):
        return str(target)
    if isinstance(target, str):
        return target
    raise ValidationError(
        f"unsupported grant scope target type: {type(target).__name__}"
    )


# --- projection source -----------------------------------------------------------

class InMemoryControlState:
    """The default projection source: process-local dicts, nothing durable.

    This is exactly what the bridge did before an injected source existed.
    It is honest about being in-memory: ``durable`` is False and the class
    writes no bytes anywhere.  A composition that has real grant/execution/
    receipt state supplies its own source object instead (duck-typed against
    this one) — see :class:`ControlBridge`.
    """

    __slots__ = ("_grants", "_executions", "_receipts", "_warnings")

    def __init__(self) -> None:
        self._grants: dict[str, GrantProjection] = {}
        self._executions: dict[str, ExecutionProjection] = {}
        self._receipts: dict[str, ReceiptProjection] = {}
        self._warnings: list[str] = []

    #: The in-memory source proves nothing across processes or restarts.
    durable = False

    def grants(self) -> tuple[GrantProjection, ...]:
        return tuple(self._grants.values())

    def executions(self) -> tuple[ExecutionProjection, ...]:
        return tuple(self._executions.values())

    def receipts(self) -> tuple[ReceiptProjection, ...]:
        return tuple(self._receipts.values())

    def warnings(self) -> tuple[str, ...]:
        return tuple(self._warnings)

    # -- mutation surface used by the bridge and the simulation hooks -----
    def add_grant(self, grant: GrantProjection) -> None:
        self._grants[grant.grant_id] = grant

    def add_receipt(self, receipt: ReceiptProjection) -> None:
        self._receipts[receipt.receipt_id] = receipt

    def add_execution(self, execution: ExecutionProjection) -> None:
        self._executions[execution.operation_id] = execution

    def get_execution(self, operation_id: str) -> ExecutionProjection | None:
        return self._executions.get(operation_id)

    def add_warning(self, warning: str) -> None:
        self._warnings.append(warning)


def _require_source_method(source: Any, name: str) -> Callable[..., Any]:
    method = getattr(source, name, None)
    if not callable(method):
        raise ValidationError(
            f"control-state source must provide a callable {name}()"
        )
    return method


# --- projection builder ----------------------------------------------------------

def project_control_state(
    grants: list[GrantProjection] | None = None,
    executions: list[ExecutionProjection] | None = None,
    receipts: list[ReceiptProjection] | None = None,
    warnings: list[str] | None = None,
) -> ControlProjection:
    """Build a ControlProjection from raw state lists.

    Pure function: no side effects, no I/O. Aggregates counts and
    derives the display state exactly as the engine would.
    """
    grant_tuple = tuple(grants or ())
    exec_tuple = tuple(executions or ())
    receipt_tuple = tuple(receipts or ())
    warning_tuple = tuple(warnings or ())

    active = 0
    queued = 0
    succeeded = 0
    failed = 0
    unknown = 0

    for exec_proj in exec_tuple:
        state = exec_proj.state
        if state in (ExecutionState.RUNNING.value, ExecutionState.APPROVED.value):
            active += 1
        elif state == ExecutionState.QUEUED.value or state == ExecutionState.PENDING.value:
            queued += 1
        elif state == ExecutionState.SUCCEEDED.value:
            succeeded += 1
        elif state in (ExecutionState.FAILED.value, ExecutionState.TIMED_OUT.value):
            if exec_proj.provider_side_outcome_unknown:
                unknown += 1
            else:
                failed += 1
        elif state == ExecutionState.UNKNOWN.value or exec_proj.provider_side_outcome_unknown:
            unknown += 1
        elif state == ExecutionState.CANCELLED.value:
            # Cancelled without a settled result counts as uncertain
            if exec_proj.provider_side_outcome_unknown:
                unknown += 1
            else:
                failed += 1

    return ControlProjection(
        projection_time=time.time(),
        grants=grant_tuple,
        executions=exec_tuple,
        receipts=receipt_tuple,
        active_count=active,
        queued_count=queued,
        succeeded_count=succeeded,
        failed_count=failed,
        unknown_count=unknown,
        integrity_warnings=warning_tuple,
    )


# --- live views over an injected state source -------------------------------------
#
# The bridge's public methods and its simulation hooks historically reached
# ``self._grants`` / ``self._executions`` / ``self._receipts`` / ``self._warnings``
# as plain containers.  These thin adapters keep that shape while delegating
# every read AND write to the injected source, so an injected source is never
# merely decorative: a projection can only show what the source holds.
# Sources that expose no mutation surface (read-only projections) fail closed
# on writes with a ValidationError instead of silently diverging.


class _SourceMirror(MutableMapping[str, Any]):
    """MutableMapping over a source's ``*_items()`` view plus mutators.

    A source may implement the granular item views (``grant_items``,
    ``execution_items``, ``receipt_items``) or just the aggregate reads
    (``grants``, ``executions``, ``receipts``); either way every read and
    write is delegated — nothing is cached locally.
    """

    _view_name = ""
    _aggregate_name = ""
    _add_name = ""
    _id_attr = ""

    def __init__(self, source: Any) -> None:
        self._source = source

    def _items(self) -> tuple[Any, ...]:
        view = getattr(self._source, self._view_name, None)
        if callable(view):
            return tuple(view())
        accessor = getattr(self._source, self._aggregate_name, None)
        if callable(accessor):
            return tuple(accessor())
        raise ValidationError(
            f"control-state source must provide {self._view_name}() or "
            f"{self._aggregate_name}()"
        )

    def __getitem__(self, key: str) -> Any:
        for item in self._items():
            if getattr(item, self._id_attr, None) == key:
                return item
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        adder = getattr(self._source, self._add_name, None)
        if not callable(adder):
            raise ValidationError(
                f"control-state source does not accept {self._add_name} writes"
            )
        adder(value)

    def __delitem__(self, key: str) -> None:
        # Removal from a projected source is not part of the v0.2 bridge
        # contract; refuse rather than pretend a delete happened.
        raise ValidationError("control-state sources do not support removal")

    def __iter__(self) -> Iterator[str]:
        for item in self._items():
            ident = getattr(item, self._id_attr, None)
            if ident is not None:
                yield ident

    def __len__(self) -> int:
        return len(self._items())


class _GrantMirror(_SourceMirror):
    _view_name = "grant_items"
    _aggregate_name = "grants"
    _add_name = "add_grant"
    _id_attr = "grant_id"


class _ExecutionMirror(_SourceMirror):
    _view_name = "execution_items"
    _aggregate_name = "executions"
    _add_name = "add_execution"
    _id_attr = "operation_id"


class _ReceiptMirror(_SourceMirror):
    _view_name = "receipt_items"
    _aggregate_name = "receipts"
    _add_name = "add_receipt"
    _id_attr = "receipt_id"


class _WarningMirror:
    """List-shaped live view over the source's warnings (append-only)."""

    def __init__(self, source: Any) -> None:
        self._source = source

    def append(self, warning: str) -> None:
        adder = getattr(self._source, "add_warning", None)
        if not callable(adder):
            raise ValidationError("control-state source does not accept warning writes")
        adder(warning)

    def __iter__(self) -> Iterator[str]:
        return iter(tuple(self._source.warnings()))

    def __len__(self) -> int:
        return len(tuple(self._source.warnings()))

    def __bool__(self) -> bool:
        return bool(tuple(self._source.warnings()))


# --- bridge ----------------------------------------------------------------------

class ControlBridge:
    """Qt-free bridge between the desktop and the v0.2 control plane.

    All reads go through ``projection_async`` (or the synchronous
    ``projection`` variant for tests). All operations go through
    ``prepare_*`` → ``approve_*`` — never directly.

    This bridge is the single seam (MUTATION_FENCE): the desktop never
    reaches past it into execution/grant/receipt internals.

    State source: when a real projection source is injected (an object
    exposing ``grants()``/``executions()``/``receipts()``/``warnings()``,
    optionally ``durable``), every projection and lookup is obtained from
    THAT source — the bridge stops projecting its own private dicts.  With
    no source, an :class:`InMemoryControlState` is used, which is exactly
    the previous behaviour and is honestly in-memory (``durable`` False);
    this class claims no durability it does not have.
    """

    def __init__(
        self,
        execution_manager: ExecutionManager | None = None,
        state_dir: Path | None = None,
        *,
        state_source: Any | None = None,
        capability_authority: CapabilityAuthority | None = None,
    ) -> None:
        if state_source is None:
            state_source = InMemoryControlState()
        else:
            for required in ("grants", "executions", "receipts", "warnings"):
                _require_source_method(state_source, required)
        self._execution = execution_manager or ExecutionManager()
        self._state_dir = state_dir
        self._closed = False
        #: The injected (or default in-memory) grant/execution/receipt source.
        self._state = state_source
        # The ONE shared capability authority. When present, approve_execution
        # issues a real grant through it; when absent, the bridge remains
        # usable for tests/state-machine exercises that do not model grants.
        self._capability_authority = capability_authority
        # Kept as live views over ``self._state`` so existing callers and
        # simulation hooks keep working; with the default source they ARE
        # the in-memory dicts (identical behaviour to before injection).
        self._grants: dict[str, GrantProjection] = _GrantMirror(self._state)
        self._executions: dict[str, ExecutionProjection] = _ExecutionMirror(self._state)
        self._receipts: dict[str, ReceiptProjection] = _ReceiptMirror(self._state)
        self._warnings: list[str] = _WarningMirror(self._state)
        self._prepared_operation: PreparedControlOperation | None = None
        self._approve_callbacks: dict[str, Callable[[PreparedControlOperation], Any]] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def state_source(self) -> Any:
        """The object this bridge projects from (injected or in-memory)."""
        return self._state

    @property
    def durable_state(self) -> bool:
        """True only when the injected source declares itself durable.

        The default in-memory source declares False; the bridge never
        infers durability from anything else.
        """
        return bool(getattr(self._state, "durable", False))

    def close(self) -> None:
        """Close the bridge and release resources."""
        if self._closed:
            return
        self._closed = True
        self._prepared_operation = None

    async def close_async(self) -> None:
        """Async close compatible with qasync event loops."""
        self.close()

    # ------------------------------------------------------------------
    # projection (read-only)
    # ------------------------------------------------------------------

    def projection(self) -> ControlProjection:
        """Synchronous projection for tests and callers with a ready state.

        Every part of the projection is read from ``self._state`` — the
        injected source when one was supplied, otherwise the in-memory
        default.  The bridge holds no shadow copy that could drift.
        """
        if self._closed:
            raise StateError("control bridge is closed")
        return project_control_state(
            grants=list(self._state.grants()),
            executions=list(self._state.executions()),
            receipts=list(self._state.receipts()),
            warnings=list(self._state.warnings()),
        )

    async def projection_async(self) -> ControlProjection:
        """Async projection, designed to be polled at bounded intervals.

        Off-loaded to the execution manager so the UI thread is never
        blocked by I/O. In this in-memory implementation it is fast;
        when backed by durable store, the execution manager owns the
        thread boundary.
        """
        if self._closed:
            raise StateError("control bridge is closed")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.projection)

    # ------------------------------------------------------------------
    # grant operations (zero-spend inspection)
    # ------------------------------------------------------------------

    def list_grants(self, scope: str | None = None) -> list[GrantProjection]:
        """List active grants, optionally filtered by scope."""
        if self._closed:
            raise StateError("control bridge is closed")
        grants = list(self._state.grants())
        if scope is not None:
            grants = [g for g in grants if g.scope == scope]
        return grants

    # ------------------------------------------------------------------
    # execution preparation (zero-spend)
    # ------------------------------------------------------------------

    def prepare_execution(
        self,
        kind: str,
        scope: str,
        description: str,
        approved_by: str,
        *,
        grant_scope: str | None = None,
        grant_scope_target: Path | EgressDestination | DirectoryScope | EgressScope | None = None,
        estimated_resources: dict[str, Any] | None = None,
        pricing_evidence: dict[str, Any] | None = None,
    ) -> PreparedControlOperation:
        """Prepare an execution operation (zero spend, no dispatch).

        Returns a ``PreparedControlOperation`` with a bound request
        digest. No executor is built, nothing is written. The caller
        must then call ``approve_execution`` to actually dispatch.

        ``grant_scope_target`` binds the approval to a concrete scope
        (a workspace ``Path``/``DirectoryScope`` or an ``EgressDestination``/
        ``EgressScope``). It is part of the request digest but is NOT spent
        until approval.
        """
        if self._closed:
            raise StateError("control bridge is closed")

        operation_id = _new_operation_id()
        request_data = {
            "operation_id": operation_id,
            "kind": kind,
            "scope": scope,
            "description": description,
            "grant_scope": grant_scope,
            "grant_scope_target": _serialize_scope_target(grant_scope_target),
            "estimated_resources": estimated_resources or {},
        }
        digest = _request_digest(request_data)

        approval = ApprovalRecord(
            approval_id=_new_approval_id(),
            operation_id=operation_id,
            scope=scope,
            approved_by=approved_by,
            approved_at=time.time(),
            request_digest=digest,
            pricing_evidence=pricing_evidence,
        )

        summary_lines = [
            f"Operation: {kind} / {scope}",
            f"Description: {description}",
            f"ID: {operation_id}",
            f"Request digest: {digest[:16]}…",
        ]
        if grant_scope:
            summary_lines.append(f"Required grant: {grant_scope}")
        if grant_scope_target is not None:
            summary_lines.append(f"Grant target: {grant_scope_target}")
        if estimated_resources:
            for key, value in estimated_resources.items():
                summary_lines.append(f"  {key}: {value}")

        prepared = PreparedControlOperation(
            operation_id=operation_id,
            kind=kind,
            scope=scope,
            description=description,
            summary="\n".join(summary_lines),
            request_digest=digest,
            approval=approval,
            grant_scope=grant_scope,
            grant_scope_target=grant_scope_target,
            operation="execute",
            estimated_resources=estimated_resources or {},
        )

        self._prepared_operation = prepared
        return prepared

    def prepare_cancellation(
        self,
        operation_id: str,
        approved_by: str,
    ) -> PreparedControlOperation:
        """Prepare a cancellation operation (zero spend)."""
        if self._closed:
            raise StateError("control bridge is closed")
        existing = self._state.get_execution(operation_id) if hasattr(self._state, "get_execution") else self._executions.get(operation_id)
        if existing is None:
            raise ValidationError(f"unknown operation: {operation_id}")

        exec_proj = existing
        if exec_proj.state in _TERMINAL_EXECUTION_STATES:
            raise ValidationError(f"operation is already terminal: {exec_proj.state}")

        request_data = {
            "operation": "cancel",
            "target_id": operation_id,
            "approved_by": approved_by,
        }
        digest = _request_digest(request_data)

        approval = ApprovalRecord(
            approval_id=_new_approval_id(),
            operation_id=operation_id,
            scope=exec_proj.scope or "cancel",
            approved_by=approved_by,
            approved_at=time.time(),
            request_digest=digest,
        )

        prepared = PreparedControlOperation(
            operation_id=operation_id,
            kind=exec_proj.kind,
            scope=exec_proj.scope or "cancel",
            description=f"Cancel operation {operation_id}",
            summary=f"Cancel operation {operation_id} ({exec_proj.description})",
            request_digest=digest,
            approval=approval,
            grant_scope=None,
            operation="cancel",
        )

        self._prepared_operation = prepared
        return prepared

    # ------------------------------------------------------------------
    # approval + dispatch
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_capability_kind(grant_scope: str | None, operation_scope: str) -> str:
        """Map a prepared operation's declared scope to a seam capability kind."""
        kind = grant_scope or operation_scope
        mapping = {
            "tool_invocation": "workspace-read",
            "workspace-read": "workspace-read",
            "workspace-write": "workspace-write",
            "process_execution": "process-run",
            "git_inspect": "git-inspect",
            "git_edit": "git-mutate",
            "git_stage": "git-mutate",
            "git_commit": "git-mutate",
            "git_push": "git-mutate",
            "git_mutation": "git-mutate",
            "network_egress": "egress",
            "egress": "egress",
            "plugin_load": "workspace-read",
        }
        return mapping.get(kind, kind)

    def _build_grant_request(self, prepared: PreparedControlOperation) -> GrantRequest:
        """Build a real GrantRequest from a prepared execution operation.

        The request is minted at approval time, never during prepare, so
        abandoned approvals leave the authority inventory unchanged.
        """
        kind = self._derive_capability_kind(prepared.grant_scope, prepared.scope)
        target = prepared.grant_scope_target
        scope: DirectoryScope | EgressScope | None = None

        if kind in ("workspace-read", "workspace-write", "process-run", "git-inspect", "git-mutate"):
            if isinstance(target, DirectoryScope):
                scope = target
            elif isinstance(target, (str, Path)):
                scope = DirectoryScope(root=Path(target))
            elif target is None:
                scope = None
            else:
                raise ValidationError(
                    f"invalid scope target for {kind}: {type(target).__name__}"
                )
        elif kind == "egress":
            if isinstance(target, EgressScope):
                scope = target
            elif isinstance(target, EgressDestination):
                scope = EgressScope(destinations=(target,), credential_reference=None)
            elif target is None:
                scope = EgressScope(destinations=(), credential_reference=None)
            else:
                raise ValidationError(
                    f"invalid scope target for egress: {type(target).__name__}"
                )

        resources = prepared.estimated_resources or {}
        ttl_seconds = float(resources.get("ttl_seconds", 300.0))
        max_effects = int(resources.get("max_effects", 1))

        return GrantRequest(
            subject=Subject(kind=prepared.kind, identity=prepared.operation_id),
            kind=kind,
            scope=scope,
            ttl_seconds=ttl_seconds,
            max_effects=max_effects,
        )

    def _mirror_grant_into_projection(self, grant: Any) -> None:
        """Mirror a freshly issued authority grant into the projection source.

        ``ControlPlaneState`` already projects live authority inventory, so
        mirroring there would duplicate and is rejected. For in-memory and
        other sources we add a projection so the bridge view stays consistent
        with the shared authority.
        """
        # Sources that read directly from the authority inventory need no
        # mirroring (and actively reject writes through add_grant).
        if hasattr(self._state, "capability_authority"):
            return

        subject = f"{grant.subject.kind}:{grant.subject.identity}"
        if isinstance(grant.scope, DirectoryScope):
            subject = str(grant.scope.root)
        elif isinstance(grant.scope, EgressScope):
            subject = ",".join(
                f"{d.scheme}://{d.host}:{d.port}"
                for d in grant.scope.destinations
            )

        projection = GrantProjection(
            grant_id=grant.grant_id,
            scope=grant.kind,
            subject=subject,
            issued_at=grant.issued_at.timestamp(),
            expires_at=None,  # monotonic deadline; wall clock unknown
            issued_by=f"capability-authority:{id(self._capability_authority):x}",
            request_digest=None,
            binding_scope=None,  # not available from capability-authority grants
            active=True,
        )
        self._grants[grant.grant_id] = projection

    def approve_execution(self, prepared: PreparedControlOperation) -> None:
        """Approve and dispatch a prepared execution operation.

        This is the ONLY place an executor is constructed and an
        operation is dispatched. When a shared ``CapabilityAuthority`` is
        configured, approval issues exactly one real grant through it and
        binds the grant to the queued execution; if the authority refuses,
        the prepared operation is consumed, no execution is queued, and a
        ``ValidationError`` surfaces the refusal.
        """
        if self._closed:
            raise StateError("control bridge is closed")
        if self._prepared_operation is None:
            raise StateError("no prepared operation")
        if self._prepared_operation.operation_id != prepared.operation_id:
            raise ApprovalInvalidatedError("prepared operation does not match current")

        # Verify the operation was prepared through this bridge
        if prepared.operation != "execute":
            raise ValidationError(f"cannot approve {prepared.operation!r} as execution")

        # Issue a real grant through the shared authority when one is
        # composed. The grant is bound to this execution; if issuance is
        # refused, the approval is consumed and no execution is queued.
        grant_id: str | None = None
        if self._capability_authority is not None:
            try:
                request = self._build_grant_request(prepared)
                grant = self._capability_authority.grant(request)
                grant_id = grant.grant_id
                self._mirror_grant_into_projection(grant)
            except (CapabilityDenied, CapabilitySystemError) as exc:
                self._prepared_operation = None
                reason = getattr(exc, "reason", None)
                reason_label = f" ({reason.value})" if reason is not None else ""
                self._warnings.append(
                    f"approval denied for {prepared.operation_id}: {exc}{reason_label}"
                )
                raise ValidationError(
                    f"capability grant refused{reason_label}: {exc}"
                ) from exc

        # Record the execution in durable state when a persistence seam is
        # attached.  The queue item id is the bridge operation_id so the
        # operation→item binding is stable and the recovery path can find it.
        if callable(getattr(self._state, "persist_execution", None)):
            from bots5.core.queue_state_machine import (
                ExecutionQueueItem,
                ExecutionQueueState,
            )

            iso_now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            queue_item = ExecutionQueueItem(
                id=prepared.operation_id,
                revision=1,
                state=ExecutionQueueState.QUEUED,
                operation_id=prepared.operation_id,
            )
            self._state.persist_execution(
                queue_item,
                operation_id=prepared.operation_id,
                approval_id=prepared.approval.approval_id,
                grant_id=grant_id,
                kind=prepared.kind,
                scope=prepared.scope,
                description=prepared.description,
            )

        # Record the execution in the projection source (durable or in-memory).
        now = time.time()
        exec_proj = ExecutionProjection(
            operation_id=prepared.operation_id,
            kind=prepared.kind,
            state=ExecutionState.QUEUED.value,
            scope=prepared.scope,
            description=prepared.description,
            submitted_at=now,
            started_at=None,
            ended_at=None,
            duration_seconds=None,
            exit_code=None,
            error_type=None,
            error_message=None,
            provider_side_outcome_unknown=True,  # Conservative: unknown until settled
            grant_id=grant_id,
            approval_id=prepared.approval.approval_id,
            output_path=None,
        )
        self._executions[prepared.operation_id] = exec_proj  # mirrors into the state source

        # Mark approval consumed
        self._prepared_operation = None

        # Dispatch via callback if registered (in-process execution)
        callback = self._approve_callbacks.get("execute")
        if callback is not None:
            callback(prepared)

    def approve_cancellation(self, prepared: PreparedControlOperation) -> None:
        """Approve and dispatch a prepared cancellation."""
        if self._closed:
            raise StateError("control bridge is closed")
        if prepared.operation != "cancel":
            raise ValidationError(f"cannot approve {prepared.operation!r} as cancellation")

        operation_id = prepared.operation_id
        exec_existing = (
            self._state.get_execution(operation_id)
            if hasattr(self._state, "get_execution")
            else self._executions.get(operation_id)
        )
        if exec_existing is None:
            raise ValidationError(f"unknown operation: {operation_id}")

        exec_proj = exec_existing
        # Persist the cancellation to durable storage when the source supports it.
        if callable(getattr(self._state, "cancel_execution", None)):
            self._state.cancel_execution(operation_id)

        # Move to cancelled; outcome remains unknown until settled
        now = time.time()
        updated = ExecutionProjection(
            operation_id=exec_proj.operation_id,
            kind=exec_proj.kind,
            state=ExecutionState.CANCELLED.value,
            scope=exec_proj.scope,
            description=exec_proj.description,
            submitted_at=exec_proj.submitted_at,
            started_at=exec_proj.started_at,
            ended_at=now,
            duration_seconds=now - exec_proj.submitted_at if exec_proj.submitted_at else None,
            exit_code=None,
            error_type=None,
            error_message="Cancelled by operator",
            provider_side_outcome_unknown=True,  # Still unknown — may have partially executed
            grant_id=exec_proj.grant_id,
            approval_id=exec_proj.approval_id,
            output_path=exec_proj.output_path,
        )
        self._executions[operation_id] = updated
        self._prepared_operation = None

        callback = self._approve_callbacks.get("cancel")
        if callback is not None:
            callback(prepared)

    # ------------------------------------------------------------------
    # test/simulation hooks (never used in production)
    # ------------------------------------------------------------------

    def _register_approve_callback(self, kind: str, callback: Callable[[PreparedControlOperation], Any]) -> None:
        """Register a callback for approve dispatch (test-only)."""
        self._approve_callbacks[kind] = callback

    def _set_execution_state(self, operation_id: str, state: ExecutionState, **kwargs: Any) -> None:
        """Directly set execution state (test/simulation only)."""
        existing = (
            self._state.get_execution(operation_id)
            if hasattr(self._state, "get_execution")
            else self._executions.get(operation_id)
        )
        if existing is None:
            raise ValueError(f"unknown operation: {operation_id}")
        now = time.time()
        updated_dict = {
            "operation_id": existing.operation_id,
            "kind": existing.kind,
            "state": state.value,
            "scope": existing.scope,
            "description": existing.description,
            "submitted_at": existing.submitted_at,
            "started_at": existing.started_at,
            "ended_at": existing.ended_at,
            "duration_seconds": existing.duration_seconds,
            "exit_code": existing.exit_code,
            "error_type": existing.error_type,
            "error_message": existing.error_message,
            "provider_side_outcome_unknown": existing.provider_side_outcome_unknown,
            "grant_id": existing.grant_id,
            "approval_id": existing.approval_id,
            "output_path": existing.output_path,
        }
        updated_dict.update(kwargs)
        self._executions[operation_id] = ExecutionProjection(**updated_dict)

    def _add_grant(self, grant: GrantProjection) -> None:
        """Add a grant projection (test/simulation only)."""
        self._grants[grant.grant_id] = grant

    def _add_receipt(self, receipt: ReceiptProjection) -> None:
        """Add a receipt projection (test/simulation only)."""
        self._receipts[receipt.receipt_id] = receipt

    def _add_warning(self, warning: str) -> None:
        """Add an integrity warning (test/simulation only)."""
        self._warnings.append(warning)
