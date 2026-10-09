"""
Capability delegation seam for B.O.T.S. v0.2.

This module provides a narrow, bounded capability/authority contract that
composes with v0.1 admission control without replacing it.

Threat model: accidental / defective / compromised-in-scope / out-of-authority
behavior. This seam makes authority explicit, bounded, enumerable and
deny-by-default; it is NOT an in-process security boundary.

Key constraints:
- v0.1 fail-closed authority (DataRootAuthority) may NOT be weakened.
- Self-authored plugins get NO ambient trust: a subject starts with zero grants.
- No universal kernel: only bounded, explicit capability kinds.
- Egress semantics: one controlled test consumer with bounded destinations.
- Grants are process-local, epoch-free: restart creates a new authority
  whose NO_GRANT world denies everything until re-granted.

Usage pattern (producer):

    authority = CapabilityAuthority()
    subject = Subject(kind="tool", identity="my-tool")
    request = GrantRequest(
        subject=subject,
        kind="workspace-read",
        scope=DirectoryScope(root=Path("/bounded/workspace")),
        ttl_seconds=3600.0,
        max_effects=100
    )
    grant = authority.grant(request)

    # Later, before performing effect:
    try:
        authority.authorize(grant, units=1)
        # Perform actual work (consumer enforces at syscalls)
    except CapabilityDenied as e:
        # Deny operation, return appropriate error to caller

Usage pattern (consumer):

    with authority.effect(grant, units=1):
        # Perform actual work; effect is tracked through settlement
        # If exception occurs, effect count is still decremented

No persistence: grants exist only in this process. No ambient contextvar grants.
No secret values: credential_reference is an opaque name, never a secret value.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, ContextManager, Iterator

from bots5.core.errors import CoreError
from bots5.domain.clock import Clock, SystemClock


# ============================================================================
# Exception hierarchy (under CoreError, v0.1 AuthorityError untouched)
# ============================================================================


class CapabilityError(CoreError):
    """Base class for capability seam failures."""


class CapabilityDenied(CapabilityError):
    """Authorization denied with structured reason."""

    def __init__(self, reason: DenialReason, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(message)


class CapabilitySystemError(CapabilityError):
    """Internal capability system error (programming or configuration)."""


# ============================================================================
# Denial reason enum
# ============================================================================


class DenialReason(str, Enum):
    """Structured denial reasons for CapabilityDenied."""

    NO_GRANT = "NO_GRANT"  # grant not issued by this authority / foreign object
    UNKNOWN_KIND = "UNKNOWN_KIND"  # unregistered kind
    NOT_FORWARD = "NOT_FORWARD"  # RELEASED/CLEANUP/REVOKED
    EXPIRED = "EXPIRED"  # deadline passed
    EXHAUSTED = "EXHAUSTED"  # budget exhausted
    SCOPE_MISMATCH = "SCOPE_MISMATCH"  # path outside scope / destination not allowed
    INVALID_UNITS = "INVALID_UNITS"  # units < 1 or > remaining
    AUTHORITY_FORKED = "AUTHORITY_FORKED"  # os.getpid() changed since construction


# ============================================================================
# Subject
# ============================================================================


@dataclass(frozen=True, slots=True)
class Subject:
    """A subject that can receive capabilities (tool, code-run, plugin, etc.)."""

    kind: str
    identity: str

    def __post_init__(self) -> None:
        # Reject empty kind/identity
        if not self.kind or not isinstance(self.kind, str):
            raise CapabilitySystemError("Subject.kind must be a non-empty string")
        if not self.identity or not isinstance(self.identity, str):
            raise CapabilitySystemError("Subject.identity must be a non-empty string")
        # Reject NUL characters
        if "\x00" in self.kind or "\x00" in self.identity:
            raise CapabilitySystemError("Subject fields must not contain NUL characters")
        # Reject control characters (ASCII 0-31, 127)
        for field_name, value in [("kind", self.kind), ("identity", self.identity)]:
            for ch in value:
                code = ord(ch)
                if code < 32 or code == 127:
                    raise CapabilitySystemError(
                        f"Subject.{field_name} must not contain control characters"
                    )


# ============================================================================
# CapabilityKind (per-authority, no global registry)
# ============================================================================


@dataclass(frozen=True, slots=True)
class CapabilityKind:
    """One capability kind with name and description."""

    name: str
    description: str


# ============================================================================
# Scope view (restricted exposure for grants)
# ============================================================================


class _ScopeView:
    """Internal: restricted view of scope for grant exposure."""

    pass


@dataclass(frozen=True, slots=True)
class _DirectoryScopeView(_ScopeView):
    """Restricted view of DirectoryScope for grant exposure."""

    root: Path


@dataclass(frozen=True, slots=True)
class _EgressScopeView(_ScopeView):
    """Restricted view of EgressScope for grant exposure."""

    destinations: tuple[EgressDestination, ...]
    has_credential_reference: bool


# ============================================================================
# Scopes
# ============================================================================


@dataclass(frozen=True, slots=True)
class DirectoryScope:
    """
    Scope for directory-bound capabilities (workspace-read/write, process-run, git-*).

    Bindings: workspace-read, workspace-write, process-run, git-inspect, git-mutate.

    Construction rules:
    - root must be absolute
    - No relative components (".")
    - No ".." components
    - Resolves+re-checks to absolute (no symlink following beyond Path.resolve())
    - Enforcement of what actually happens on disk is the consumer's obligation.
    """

    root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise CapabilitySystemError("DirectoryScope.root must be a Path")
        # Check if original path is absolute
        if not self.root.is_absolute():
            raise CapabilitySystemError("DirectoryScope.root must be an absolute path")
        # Reject ".." components in original path
        parts = self.root.parts
        for part in parts:
            if part == "..":
                raise CapabilitySystemError("DirectoryScope.root must not contain '..'")
        # Normalize the path
        normalized = self.root.resolve()
        # Must be absolute after resolution
        if not normalized.is_absolute():
            raise CapabilitySystemError("DirectoryScope.root must be an absolute path")
        # Reject empty components (from "//" or trailing "/")
        normalized_parts = normalized.parts
        if any(p == "" for p in normalized_parts):
            raise CapabilitySystemError("DirectoryScope.root must not have empty path components")
        # Re-assign with normalized value
        object.__setattr__(self, "root", normalized)

    def allows_path(self, path: Path) -> bool:
        """Check if a path is within this scope (normalized prefix check)."""
        if not isinstance(path, Path):
            return False
        try:
            normalized = path.resolve()
        except (OSError, ValueError):
            return False
        # Must be absolute
        if not normalized.is_absolute():
            return False
        # Must be under root (prefix check on normalized path)
        try:
            normalized.relative_to(self.root)
            return True
        except ValueError:
            return False


@dataclass(frozen=True, slots=True)
class EgressDestination:
    """
    One egress destination: (scheme, host, port).

    Exact-match triple; port in 1..65535.
    """

    scheme: str
    host: str
    port: int

    def __post_init__(self) -> None:
        if not self.scheme or not isinstance(self.scheme, str):
            raise CapabilitySystemError("EgressDestination.scheme must be a non-empty string")
        if not self.host or not isinstance(self.host, str):
            raise CapabilitySystemError("EgressDestination.host must be a non-empty string")
        if not isinstance(self.port, int) or self.port < 1 or self.port > 65535:
            raise CapabilitySystemError("EgressDestination.port must be an integer in 1..65535")
        # Reject NUL characters
        if "\x00" in self.scheme or "\x00" in self.host:
            raise CapabilitySystemError("EgressDestination fields must not contain NUL characters")
        # Reject control characters
        for field_name, value in [("scheme", self.scheme), ("host", self.host)]:
            for ch in value:
                code = ord(ch)
                if code < 32 or code == 127:
                    raise CapabilitySystemError(
                        f"EgressDestination.{field_name} must not contain control characters"
                    )


@dataclass(frozen=True, slots=True)
class EgressScope:
    """
    Scope for egress capabilities.

    destinations: tuple of allowed (scheme, host, port) triples.
        Empty tuple == "no network" (explicit deny for all egress).
    credential_reference: opaque name only; never a secret value.
    """

    destinations: tuple[EgressDestination, ...]
    credential_reference: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.destinations, tuple):
            raise CapabilitySystemError("EgressScope.destinations must be a tuple")
        for dest in self.destinations:
            if not isinstance(dest, EgressDestination):
                raise CapabilitySystemError("EgressScope.destinations elements must be EgressDestination")
        if self.credential_reference is not None:
            if not isinstance(self.credential_reference, str):
                raise CapabilitySystemError("EgressScope.credential_reference must be a string or None")
            if "\x00" in self.credential_reference:
                raise CapabilitySystemError("EgressScope.credential_reference must not contain NUL characters")
            for ch in self.credential_reference:
                code = ord(ch)
                if code < 32 or code == 127:
                    raise CapabilitySystemError(
                        "EgressScope.credential_reference must not contain control characters"
                    )


# ============================================================================
# Grant request and status enum
# ============================================================================


@dataclass(frozen=True, slots=True)
class GrantRequest:
    """
    Request to issue a new grant.

    subject: Subject requesting capability.
    kind: registered capability kind name.
    scope: DirectoryScope or EgressScope or None (kind-specific).
    ttl_seconds: finite positive number (deadline), required.
    max_effects: positive int (unit budget), required.
    """

    subject: Subject
    kind: str
    scope: DirectoryScope | EgressScope | None
    ttl_seconds: float
    max_effects: int

    def __post_init__(self) -> None:
        if not isinstance(self.subject, Subject):
            raise CapabilitySystemError("GrantRequest.subject must be a Subject")
        if not isinstance(self.kind, str) or not self.kind:
            raise CapabilitySystemError("GrantRequest.kind must be a non-empty string")
        if self.scope is not None:
            if not isinstance(self.scope, (DirectoryScope, EgressScope)):
                raise CapabilitySystemError(
                    "GrantRequest.scope must be DirectoryScope, EgressScope, or None"
                )
        # F2: ttl_seconds MUST be finite positive (no None)
        if not isinstance(self.ttl_seconds, (int, float)) or self.ttl_seconds <= 0:
            raise CapabilitySystemError("GrantRequest.ttl_seconds must be a finite positive number")
        import math
        if math.isnan(self.ttl_seconds) or math.isinf(self.ttl_seconds):
            raise CapabilitySystemError("GrantRequest.ttl_seconds must be a finite number")
        # F2: max_effects MUST be positive int (no None)
        if not isinstance(self.max_effects, int) or self.max_effects <= 0:
            raise CapabilitySystemError("GrantRequest.max_effects must be a positive integer")


class GrantStatus(str, Enum):
    """
    Lifecycle status for a grant.

    FORWARD: usable (initial)
    CLEANUP: revocation in progress (in-flight effects exist)
    RELEASED: early surrender (no in-flight effects)
    EXPIRED: terminal (deadline passed, never resurrected)
    EXHAUSTED: terminal (budget exhausted, never resurrected)
    REVOKED: terminal (revoked, no in-flight effects) or cleanup completed
    """

    FORWARD = "FORWARD"
    CLEANUP = "CLEANUP"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    EXHAUSTED = "EXHAUSTED"
    REVOKED = "REVOKED"


# ============================================================================
# CapabilityGrant
# ============================================================================


@dataclass(slots=True)
class CapabilityGrant:
    """
    One capability grant issued by a CapabilityAuthority.

    grant_id: uuid4 hex
    subject: the subject granted capability
    kind: registered capability kind name
    scope: DirectoryScope or EgressScope or None (kind-specific)
    issued_at: wall clock, evidence-only (NOT expiry authority — deadline is)
    deadline: monotonic deadline (REQUIRED: no None)
    remaining_effects: positive int (unit budget, REQUIRED: no None)
    status: GrantStatus

    Lifecycle: FORWARD → {RELEASED, CLEANUP} → terminal below
    - release() → RELEASED (early surrender)
    - revoke() → CLEANUP if in-flight effects exist, else REVOKED
    - CLEANUP flips to REVOKED when the last in-flight effect settles
    - CLEANUP may also flip to RELEASED when deferred outcome is "RELEASED"
    EXPIRED / EXHAUSTED are observed terminal states (set lazily at check time)
    """

    grant_id: str
    subject: Subject
    kind: str
    scope: DirectoryScope | EgressScope | None
    issued_at: datetime
    deadline: float  # REQUIRED: no None
    remaining_effects: int  # REQUIRED: no None
    status: GrantStatus = GrantStatus.FORWARD
    _scope_view: _ScopeView | None = field(default=None, repr=False, compare=False)
    _deferred: str | None = field(default=None, repr=False, compare=False)  # "RELEASED" or "REVOKED" when CLEANUP

    def expires_in(self, clock: Clock) -> float:
        """
        Time remaining until deadline in seconds (monotonic).

        Returns >= 0 if deadline is in the future, 0 if already passed.
        This is a convenience; the authoritative check is via _is_deadline_passed.
        """
        remaining = self.deadline - clock.monotonic()
        return max(0.0, remaining)


# ============================================================================
# AuditEntry
# ============================================================================


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One audit log entry for decisions."""

    seq: int  # monotonic sequence number
    timestamp: datetime  # wall clock
    grant_id: str
    subject_kind: str
    subject_identity: str
    kind: str
    reason: str  # "OK" or denial reason string


# ============================================================================
# CapabilityAuthority
# ============================================================================


class CapabilityAuthority:
    """
    The authority instance is the issuance anchor: constructing one is a product-bootstrap act.

    No ambient grant, no contextvar magic, no ambient bypass: any code path that
    did not obtain a grant object from THIS authority instance is denied with NO_GRANT.

    Thread-safety: one internal lock; checks-and-decrements are atomic.
    No waiting/condition — denial is immediate.

    Composition with v0.1:
    - The seam NEVER touches DataRootAuthority.
    - It neither holds nor bypasses v0.1 admission.
    - Consumers performing effects against the data root must hold their v0.1
      admission exactly as today; the seam only decides whether the consumer's
      delegated capability exists and is within scope/budget.
    """

    # Built-in v0.2 kinds (per-authority, not global registry)
    _BUILTIN_KINDS = {
        "workspace-read": CapabilityKind(name="workspace-read", description="read-only local tool inspecting an explicitly granted workspace"),
        "workspace-write": CapabilityKind(name="workspace-write", description="generated code/process execution inside an owned bounded workspace"),
        "process-run": CapabilityKind(name="process-run", description="launching a bounded process against a bounded workspace"),
        "git-inspect": CapabilityKind(name="git-inspect", description="Git inspect/status/diff authority"),
        "git-mutate": CapabilityKind(name="git-mutate", description="consequential Git stage/commit/push authority"),
        "egress": CapabilityKind(name="egress", description="one controlled egress consumer proving bounded egress semantics"),
    }

    def __init__(self, *, clock: Clock = SystemClock(), audit_capacity: int = 1024,
                 extra_kinds: tuple[CapabilityKind, ...] = ()) -> None:
        """
        Initialize a new authority instance.

        extra_kinds: additional capability kinds to register per this authority
                     (name shape + control-char rules, duplicate across built-ins
                     and extras rejected, built-in override rejected).

        audit_capacity: bounded append-only decision log capacity (floor 1).
        """
        if audit_capacity < 1:
            audit_capacity = 1
        self._clock = clock
        self._audit_capacity = audit_capacity
        self._lock = threading.Lock()
        self._seq = 0
        self._audit_log: deque[AuditEntry] = deque(maxlen=audit_capacity)
        self._grants: dict[str, CapabilityGrant] = {}
        self._in_flight: dict[str, int] = {}  # grant_id -> in-flight effect count
        self._pid = os.getpid()

        # Build per-authority kind registry
        self._kinds: dict[str, CapabilityKind] = dict(self._BUILTIN_KINDS)
        for kind in extra_kinds:
            if not kind.name or not isinstance(kind.name, str):
                raise CapabilitySystemError("CapabilityKind.name must be a non-empty string")
            if not kind.description or not isinstance(kind.description, str):
                raise CapabilitySystemError("CapabilityKind.description must be a non-empty string")
            if "\x00" in kind.name or "\x00" in kind.description:
                raise CapabilitySystemError("CapabilityKind fields must not contain NUL characters")
            for field_name, value in [("name", kind.name), ("description", kind.description)]:
                for ch in value:
                    code = ord(ch)
                    if code < 32 or code == 127:
                        raise CapabilitySystemError(
                            f"CapabilityKind.{field_name} must not contain control characters"
                        )
            if kind.name in self._kinds:
                raise CapabilitySystemError(
                    f"CapabilityKind '{kind.name}' is already registered (duplicate or built-in override)"
                )
            self._kinds[kind.name] = kind

    def _check_pid(self) -> None:
        """Check that we're still in the original process (fail-closed on fork)."""
        if os.getpid() != self._pid:
            raise CapabilityDenied(
                reason=DenialReason.AUTHORITY_FORKED,
                message="Authority constructed in a different process (fork detected)"
            )

    def _next_seq(self) -> int:
        """Increment and return the monotonic sequence number."""
        self._seq += 1
        return self._seq

    def _append_audit(
        self,
        grant: CapabilityGrant,
        reason: str,
    ) -> None:
        """Append one audit entry."""
        entry = AuditEntry(
            seq=self._next_seq(),
            timestamp=self._clock.now(),
            grant_id=grant.grant_id,
            subject_kind=grant.subject.kind,
            subject_identity=grant.subject.identity,
            kind=grant.kind,
            reason=reason,
        )
        self._audit_log.append(entry)

    def _is_deadline_passed(self, grant: CapabilityGrant) -> bool:
        """Check if the monotonic deadline has passed."""
        return self._clock.monotonic() >= grant.deadline

    def _check_usable(self, grant: CapabilityGrant) -> tuple[bool, DenialReason | None, str | None]:
        """
        Check if a grant is usable (not terminal, not expired, not exhausted).

        Returns (usable, denial_reason, message).

        R2 (manager closure): EXPIRED/EXHAUSTED are recorded on grant.status at
        observation time so every check path (authorize, effect, inventory)
        leaves the lifecycle truthful. Callers hold the authority lock, so the
        mutation is atomic with the decision. Once recorded, the state keeps
        reporting its specific reason: the brief binds NOT_FORWARD to
        RELEASED/CLEANUP/REVOKED only; EXPIRED/EXHAUSTED remain their own
        reasons for recorded states.
        """
        if grant.status is GrantStatus.EXPIRED:
            return False, DenialReason.EXPIRED, "Grant deadline has passed (recorded expired)"
        if grant.status is GrantStatus.EXHAUSTED:
            return False, DenialReason.EXHAUSTED, "Grant budget is exhausted (recorded exhausted)"
        if grant.status is not GrantStatus.FORWARD:
            return False, DenialReason.NOT_FORWARD, f"Grant status is {grant.status.value}, not FORWARD"

        if self._is_deadline_passed(grant):
            grant.status = GrantStatus.EXPIRED
            return False, DenialReason.EXPIRED, "Grant deadline has passed"

        if grant.remaining_effects <= 0:
            grant.status = GrantStatus.EXHAUSTED
            return False, DenialReason.EXHAUSTED, "Grant budget is exhausted"

        return True, None, None

    def grant(self, request: GrantRequest) -> CapabilityGrant:
        """
        Issue a new grant from a request (explicit issuance; deny-by-default world).

        Returns the CapabilityGrant if the kind is registered.
        Raises CapabilityDenied for unknown kind.
        """
        self._check_pid()

        # Get the registered kind
        if request.kind not in self._kinds:
            raise CapabilityDenied(
                reason=DenialReason.UNKNOWN_KIND,
                message=f"Unknown capability kind: {request.kind!r}"
            )

        with self._lock:
            grant_id = uuid.uuid4().hex
            issued_at = self._clock.now()

            # Compute deadline
            deadline = self._clock.monotonic() + request.ttl_seconds

            grant = CapabilityGrant(
                grant_id=grant_id,
                subject=request.subject,
                kind=request.kind,
                scope=request.scope,
                issued_at=issued_at,
                deadline=deadline,
                remaining_effects=request.max_effects,
                status=GrantStatus.FORWARD,
            )

            # Build restricted scope view for grant exposure
            if request.scope is None:
                grant._scope_view = None
            elif isinstance(request.scope, DirectoryScope):
                grant._scope_view = _DirectoryScopeView(root=request.scope.root)
            elif isinstance(request.scope, EgressScope):
                grant._scope_view = _EgressScopeView(
                    destinations=request.scope.destinations,
                    has_credential_reference=request.scope.credential_reference is not None
                )

            self._grants[grant_id] = grant
            self._in_flight[grant_id] = 0
            self._append_audit(grant, "OK")
            return grant

    def authorize(self, grant: CapabilityGrant, *, units: int = 1,
                  target: Path | EgressDestination | None = None) -> None:
        """
        Check + consume budget; raises CapabilityDenied.

        - grant must be issued by this authority (NO_GRANT if foreign)
        - grant must be FORWARD (NOT_FORWARD if terminal)
        - deadline must be in the future (EXPIRED if passed)
        - remaining_effects must be >= units (EXHAUSTED if insufficient)
        - scope must match (SCOPE_MISMATCH if path outside DirectoryScope root
          or destination not in EgressScope allowlist)

        For DirectoryScope grants, target may be None (whole-scope effect) or a Path.
        For EgressScope grants, target is REQUIRED and must match an allowed destination.
        """
        self._check_pid()

        with self._lock:
            # Check grant is issued by this authority
            if grant.grant_id not in self._grants:
                raise CapabilityDenied(
                    reason=DenialReason.NO_GRANT,
                    message="Grant not issued by this authority (foreign grant object)"
                )

            # Check status and budget
            usable, reason, msg = self._check_usable(grant)
            if not usable:
                self._append_audit(grant, reason.value)
                raise CapabilityDenied(reason=reason, message=msg)

            # Check units
            if units < 1:
                self._append_audit(grant, DenialReason.INVALID_UNITS.value)
                raise CapabilityDenied(
                    reason=DenialReason.INVALID_UNITS,
                    message=f"units must be >= 1, got {units}"
                )
            if units > grant.remaining_effects:
                self._append_audit(grant, DenialReason.INVALID_UNITS.value)
                raise CapabilityDenied(
                    reason=DenialReason.INVALID_UNITS,
                    message=f"units ({units}) exceeds remaining_effects ({grant.remaining_effects})"
                )

            # Scope validation with target
            if grant.scope is not None:
                scope_denied, scope_msg = self._validate_scope(
                    grant.scope, target, grant.kind
                )
                if scope_denied:
                    self._append_audit(grant, DenialReason.SCOPE_MISMATCH.value)
                    raise CapabilityDenied(
                        reason=DenialReason.SCOPE_MISMATCH,
                        message=scope_msg
                    )

            # Consume budget
            grant.remaining_effects -= units

            self._append_audit(grant, "OK")

    def _validate_scope(
        self, scope: DirectoryScope | EgressScope, target: Path | EgressDestination | None,
        kind: str
    ) -> tuple[bool, str]:
        """
        Validate target against scope. Returns (denied, message).
        """
        if isinstance(scope, DirectoryScope):
            # DirectoryScope: target may be None (whole-scope effect) or a Path
            if target is None:
                # Whole-scope effect allowed
                return False, ""
            if not isinstance(target, Path):
                return True, "DirectoryScope grant requires Path target (got {type(target).__name__})"
            # R1 (manager closure): reject relative targets BEFORE resolution.
            # Resolving first would make the decision CWD-dependent (a relative
            # string could pass or fail depending on the process working
            # directory). Fail-closed requires a deterministic decision.
            if not target.is_absolute():
                return True, f"Target path must be absolute (got {target})"
            # Normalize and resolve
            try:
                normalized = target.resolve()
            except (OSError, ValueError):
                return True, f"Invalid target path: {target}"
            # Must be under root
            try:
                normalized.relative_to(scope.root)
                return False, ""
            except ValueError:
                return True, f"Target {normalized} is outside scope root {scope.root}"
        elif isinstance(scope, EgressScope):
            # EgressScope: target is REQUIRED
            if target is None:
                return True, "EgressScope grant requires a target (EgressDestination)"
            if not isinstance(target, EgressDestination):
                return True, f"EgressScope grant requires EgressDestination target (got {type(target).__name__})"
            # Must match one of the allowed destinations (exact triple)
            if not scope.destinations:
                return True, "EgressScope has no allowed destinations (empty scope)"
            for dest in scope.destinations:
                if (dest.scheme == target.scheme and
                    dest.host == target.host and
                    dest.port == target.port):
                    return False, ""
            return True, f"Target {target} not in allowed destinations"

        # None scope (kind-specific)
        return False, ""

    @contextmanager
    def effect(self, grant: CapabilityGrant, *, units: int = 1,
               target: Path | EgressDestination | None = None,
               kind: str | None = None) -> Iterator[None]:
        """
        Authorize + in-flight retention through settlement.

        This is a context manager that:
        1. Authorizes (checks + consumes budget)
        2. Tracks the effect as in-flight
        3. Ensures settlement (decrements in-flight count)

        ``kind`` optionally binds the effect to the capability kind the consumer
        believes it holds. When supplied it MUST equal ``grant.kind``; a
        mismatched kind is refused before any budget is consumed. Consumers that
        perform a specific class of action (a read-only tool, a bounded process,
        an egress send) should always pass their kind so that holding one kind of
        grant can never authorise a different kind of effect.
        """
        with self._lock:
            # Check grant is issued by this authority
            if grant.grant_id not in self._grants:
                raise CapabilityDenied(
                    reason=DenialReason.NO_GRANT,
                    message="Grant not issued by this authority (foreign grant object)"
                )

            # Kind binding: an explicit expected kind must match the grant's kind.
            if kind is not None and kind != grant.kind:
                self._append_audit(grant, DenialReason.UNKNOWN_KIND.value)
                raise CapabilityDenied(
                    reason=DenialReason.UNKNOWN_KIND,
                    message=(
                        f"effect requires kind {kind!r} but the grant authorises "
                        f"{grant.kind!r}"
                    ),
                )

            # Check status and budget
            usable, reason, msg = self._check_usable(grant)
            if not usable:
                self._append_audit(grant, reason.value)
                raise CapabilityDenied(reason=reason, message=msg)

            # Check units
            if units < 1:
                self._append_audit(grant, DenialReason.INVALID_UNITS.value)
                raise CapabilityDenied(
                    reason=DenialReason.INVALID_UNITS,
                    message=f"units must be >= 1, got {units}"
                )
            if units > grant.remaining_effects:
                self._append_audit(grant, DenialReason.INVALID_UNITS.value)
                raise CapabilityDenied(
                    reason=DenialReason.INVALID_UNITS,
                    message=f"units ({units}) exceeds remaining_effects ({grant.remaining_effects})"
                )

            # Scope validation with target
            if grant.scope is not None:
                scope_denied, scope_msg = self._validate_scope(
                    grant.scope, target, grant.kind
                )
                if scope_denied:
                    self._append_audit(grant, DenialReason.SCOPE_MISMATCH.value)
                    raise CapabilityDenied(
                        reason=DenialReason.SCOPE_MISMATCH,
                        message=scope_msg
                    )

            # Consume budget
            grant.remaining_effects -= units

            # Record the admitted effect in the audit log.
            self._append_audit(grant, "OK")

            # Track in-flight
            self._in_flight[grant.grant_id] = self._in_flight.get(grant.grant_id, 0) + units

        # Yield control to caller
        try:
            yield
        finally:
            # Always decrement in-flight count on settlement
            with self._lock:
                current = self._in_flight.get(grant.grant_id, 0)
                if current > 0:
                    self._in_flight[grant.grant_id] = current - units
                # F6: Check if cleanup should advance based on deferred outcome
                if grant.status is GrantStatus.CLEANUP:
                    if self._in_flight.get(grant.grant_id, 0) == 0:
                        # Settle to deferred outcome (RELEASED or REVOKED)
                        deferred = grant._deferred
                        if deferred == "RELEASED":
                            grant.status = GrantStatus.RELEASED
                            self._append_audit(grant, "CLEANUP→RELEASED")
                        elif deferred == "REVOKED":
                            grant.status = GrantStatus.REVOKED
                            self._append_audit(grant, "CLEANUP→REVOKED")
                        else:
                            # Default to REVOKED if no deferred outcome set
                            grant.status = GrantStatus.REVOKED
                            self._append_audit(grant, "CLEANUP→REVOKED")

    def renew(self, grant: CapabilityGrant, *, extend_ttl_seconds: float | None = None,
              add_effects: int | None = None) -> None:
        """
        Grant extension semantics.

        Applies ONLY to a grant whose current status is FORWARD and whose deadline
        (if any) is still in the future: extending after expiry is a denial (EXPIRED);
        the correct path is a new explicit grant.

        extend_ttl_seconds: moves the monotonic deadline later (never earlier).
        add_effects: increases the remaining budget (never decreases).

        Both are denied (INVALID_UNITS) if they would produce a negative/zero-live state.
        """
        self._check_pid()

        with self._lock:
            # Check grant is issued by this authority
            if grant.grant_id not in self._grants:
                raise CapabilityDenied(
                    reason=DenialReason.NO_GRANT,
                    message="Grant not issued by this authority (foreign grant object)"
                )

            # Check status is FORWARD
            if grant.status is not GrantStatus.FORWARD:
                self._append_audit(grant, DenialReason.NOT_FORWARD.value)
                raise CapabilityDenied(
                    reason=DenialReason.NOT_FORWARD,
                    message=f"Grant status is {grant.status.value}, not FORWARD (cannot renew)"
                )

            # Check deadline (if any) is still in the future
            if self._is_deadline_passed(grant):
                self._append_audit(grant, DenialReason.EXPIRED.value)
                raise CapabilityDenied(
                    reason=DenialReason.EXPIRED,
                    message="Grant deadline has passed (cannot renew; issue new grant)"
                )

            # Apply extensions
            if extend_ttl_seconds is not None:
                if not isinstance(extend_ttl_seconds, (int, float)) or extend_ttl_seconds <= 0:
                    self._append_audit(grant, DenialReason.INVALID_UNITS.value)
                    raise CapabilityDenied(
                        reason=DenialReason.INVALID_UNITS,
                        message="extend_ttl_seconds must be a positive number"
                    )
                # Move deadline later from current deadline
                new_deadline = grant.deadline + extend_ttl_seconds
                # Verify the new deadline is actually later
                if new_deadline <= grant.deadline:
                    self._append_audit(grant, DenialReason.INVALID_UNITS.value)
                    raise CapabilityDenied(
                        reason=DenialReason.INVALID_UNITS,
                        message="extend_ttl_seconds must move deadline later (narrowing is done by revoke)"
                    )
                grant.deadline = new_deadline

            if add_effects is not None:
                if not isinstance(add_effects, int) or add_effects <= 0:
                    self._append_audit(grant, DenialReason.INVALID_UNITS.value)
                    raise CapabilityDenied(
                        reason=DenialReason.INVALID_UNITS,
                        message="add_effects must be a positive integer"
                    )
                # Increase budget
                grant.remaining_effects += add_effects

            self._append_audit(grant, "OK")

    def release(self, grant: CapabilityGrant) -> None:
        """
        Early surrender of a grant.

        - FORWARD → RELEASED (no in-flight effects)
        - With in-flight effects: CLEANUP first, set deferred outcome to "RELEASED"
        """
        self._check_pid()

        with self._lock:
            # Check grant is issued by this authority
            if grant.grant_id not in self._grants:
                raise CapabilityDenied(
                    reason=DenialReason.NO_GRANT,
                    message="Grant not issued by this authority (foreign grant object)"
                )

            # Check status
            if grant.status is not GrantStatus.FORWARD:
                self._append_audit(grant, DenialReason.NOT_FORWARD.value)
                raise CapabilityDenied(
                    reason=DenialReason.NOT_FORWARD,
                    message=f"Grant status is {grant.status.value}, not FORWARD (cannot release)"
                )

            # Check in-flight
            in_flight = self._in_flight.get(grant.grant_id, 0)
            if in_flight > 0:
                grant.status = GrantStatus.CLEANUP
                grant._deferred = "RELEASED"
                self._append_audit(grant, "FORWARD→CLEANUP (deferred: RELEASED)")
            else:
                grant.status = GrantStatus.RELEASED
                self._append_audit(grant, "FORWARD→RELEASED")

    def revoke(self, grant: CapabilityGrant, reason: str) -> None:
        """
        Immediate revocation with reason.

        - FORWARD → CLEANUP if in-flight effects exist (via _in_flight counter)
          else REVOKED
        - Already CLEANUP: override deferred outcome to REVOKED
        - Already CLEANUP with no in-flight: flip to REVOKED
        - Already RELEASED/EXPIRED/EXHAUSTED/REVOKED: record audit and no-op
        """
        self._check_pid()

        with self._lock:
            # Check grant is issued by this authority
            if grant.grant_id not in self._grants:
                raise CapabilityDenied(
                    reason=DenialReason.NO_GRANT,
                    message="Grant not issued by this authority (foreign grant object)"
                )

            in_flight = self._in_flight.get(grant.grant_id, 0)
            if grant.status is GrantStatus.FORWARD:
                if in_flight > 0:
                    grant.status = GrantStatus.CLEANUP
                    grant._deferred = "REVOKED"
                    self._append_audit(grant, f"FORWARD→CLEANUP (deferred: REVOKED, revoke: {reason})")
                else:
                    grant.status = GrantStatus.REVOKED
                    self._append_audit(grant, f"FORWARD→REVOKED (revoke: {reason})")
            elif grant.status is GrantStatus.CLEANUP:
                # Override deferred outcome to REVOKED
                grant._deferred = "REVOKED"
                if in_flight == 0:
                    grant.status = GrantStatus.REVOKED
                    self._append_audit(grant, f"CLEANUP→REVOKED (revoke: {reason})")
                else:
                    self._append_audit(grant, f"CLEANUP (revoke: {reason}, deferred: REVOKED)")
            else:
                # RELEASED/EXPIRED/EXHAUSTED/REVOKED: no-op, just audit
                self._append_audit(grant, f"revoked: {reason}")

    def inventory(self) -> tuple[CapabilityGrant, ...]:
        """
        Return live (non-terminal) grants as a tuple.

        EXPIRED/EXHAUSTED are recorded lazily at check time, so they are excluded.
        """
        self._check_pid()

        with self._lock:
            result = []
            for g in self._grants.values():
                # F7: Check for lazy EXPIRED/EXHAUSTED
                if g.status is not GrantStatus.FORWARD:
                    # Already terminal, skip
                    continue
                # Check lazily for EXPIRED/EXHAUSTED
                usable, reason, _ = self._check_usable(g)
                if not usable:
                    # Set status lazily to proper GrantStatus
                    if reason is DenialReason.EXPIRED:
                        g.status = GrantStatus.EXPIRED
                    elif reason is DenialReason.EXHAUSTED:
                        g.status = GrantStatus.EXHAUSTED
                    # Exclude from inventory
                    continue
                result.append(g)
            return tuple(result)

    def audit_trail(self) -> tuple[AuditEntry, ...]:
        """Return the bounded append-only decision log as a tuple."""
        self._check_pid()

        with self._lock:
            return tuple(self._audit_log)


# ============================================================================
# Convenience: export built-in kind names as module constants
# ============================================================================

# Built-in capability kinds (as module attributes for convenience)
# These are the v0.2 built-ins defined in CapabilityAuthority._BUILTIN_KINDS

WORKSPACE_READ = "workspace-read"
"""Capability kind: read-only local tool inspecting an explicitly granted workspace."""

WORKSPACE_WRITE = "workspace-write"
"""Capability kind: generated code/process execution inside an owned bounded workspace."""

PROCESS_RUN = "process-run"
"""Capability kind: launching a bounded process against a bounded workspace."""

GIT_INSPECT = "git-inspect"
"""Capability kind: Git inspect/status/diff authority."""

GIT_MUTATE = "git-mutate"
"""Capability kind: consequential Git stage/commit/push authority."""

EGRESS = "egress"
"""Capability kind: one controlled egress consumer proving bounded egress semantics."""

__all__ = [
    # Exceptions
    "CapabilityError",
    "CapabilityDenied",
    "CapabilitySystemError",
    # DenialReason
    "DenialReason",
    # Data classes
    "Subject",
    "CapabilityKind",
    "DirectoryScope",
    "EgressDestination",
    "EgressScope",
    "GrantRequest",
    "CapabilityGrant",
    "AuditEntry",
    # Status enum
    "GrantStatus",
    # Authority
    "CapabilityAuthority",
    # Built-in kind names (module constants)
    "WORKSPACE_READ",
    "WORKSPACE_WRITE",
    "PROCESS_RUN",
    "GIT_INSPECT",
    "GIT_MUTATE",
    "EGRESS",
]
