# Capability Seam v0.2

## Purpose

This module provides a narrow, bounded capability/authority contract that composes with v0.1 admission control without replacing it. It enables explicit delegation of capabilities to tools, plugins, and other subjects while maintaining the fail-closed security model of the v0.1 authority.

## Threat Model

This seam addresses accidental / defective / compromised-in-scope / out-of-authority behavior. It makes authority explicit, bounded, enumerable and deny-by-default.

**Important**: This is NOT an in-process security boundary. It is a policy and accounting layer that works in concert with v0.1 admission control.

## Key Constraints

- v0.1 fail-closed authority (`DataRootAuthority` / `_EffectGrant` semantics) may NOT be weakened
- Self-authored plugins get NO ambient trust: a subject starts with zero grants
- No universal kernel: only bounded, explicit capability kinds
- Egress semantics: one controlled test consumer with bounded destinations
- Grants are process-local, epoch-free: restart creates a new authority whose `NO_GRANT` world denies everything until re-granted
- **Extension is per-authority**: `extra_kinds` tuple on `CapabilityAuthority.__init__()`; NO global registry or module-level `register_capability_kind()`

## Built-in Capability Kinds

| Kind | Description |
|------|-------------|
| `workspace-read` | Read-only local tool inspecting an explicitly granted workspace |
| `workspace-write` | Generated code/process execution inside an owned bounded workspace |
| `process-run` | Launching a bounded process against a bounded workspace |
| `git-inspect` | Git inspect/status/diff authority |
| `git-mutate` | Consequential Git stage/commit/push authority (separate grant; never implied by `git-inspect`) |
| `egress` | One controlled egress consumer proving bounded egress semantics |

## Lifecycle Diagram

```
FORWARD (initial, usable)
  │
  ├─► release() ──────────────► RELEASED (early surrender, no in-flight)
  │
  ├─► revoke(reason) ─────────► CLEANUP (in-flight effects exist)
  │                              │
  │                              ├─► (deferred: RELEASED) ──► RELEASED
  │                              │
  │                              └─► settle effect() ──► REVOKED
  │                                (or override by revoke)
  │
  ├─► deadline passed ────────► EXPIRED (terminal, never resurrected)
  │                               (recorded lazily on check)
  │
  └─► budget exhausted ───────► EXHAUSTED (terminal, never resurrected)
                                  (recorded lazily on check)
```

**Notes:**
- `CLEANUP` may settle to `RELEASED` (if `release()` set deferred outcome) or `REVOKED` (default or overridden by `revoke()`)
- `EXPIRED` / `EXHAUSTED` are recorded lazily on check time (in `inventory()` and authorization checks)

## Fail-Closed Rules

1. **Deny by default**: Any code path that did not obtain a grant object from THIS authority instance is denied with `NO_GRANT`

2. **No ambient grants**: No contextvar magic, no ambient bypass

3. **Process-local**: Grants exist only in this process; restart creates a new authority

4. **No persistence**: Grants are not persisted across process restarts

5. **No secrets**: `credential_reference` is an opaque name only; never a secret value

6. **Unit definition**: one authorized effect of the grant's kind; consumers authorize per actual effect with the correct grant (mislabeling is defective-consumer behavior exposed by audit)

7. **Composition**: the seam NEVER substitutes v0.1 DataRootAuthority admission; consumers must hold v0.1 admission for data-root effects exactly as today

## Consumer Obligations

1. **Obtain grants explicitly**: Use `CapabilityAuthority.grant()` to obtain grants before performing operations

2. **Authorize before use**: Call `CapabilityAuthority.authorize()` before performing the actual operation

3. **Handle denials**: Catch `CapabilityDenied` exceptions and return appropriate errors to callers

4. **Use effect() for in-flight tracking**: Wrap operations in `with authority.effect(grant)` to track effects through settlement

5. **Enforce at syscalls**: The seam decides; tools/code-git workstreams enforce at their syscalls

6. **Hold v0.1 admission**: For effects against the data root, you must hold v0.1 admission exactly as today

## Explicit Non-Goals

This module does NOT:

- Edit `data_root_authority.py`, `ports.py`, `application.py`, migrations, or desktop
- Provide persistence of grants
- Provide cross-process/forked sharing
- Enforce mechanics (no fds, no seccomp, no chroot): the seam decides; the tools enforce at syscalls
- Provide a universal kernel: no generic "any effect" kind, no policy DSL, no rule engine, no dynamic permission algebra, no ambient contextvar grants
- Weaken v0.1: zero bytes changed under v0.1 authority paths

## API Reference

### Exceptions

```python
class CapabilityError(CoreError):
    """Base class for capability seam failures."""

class CapabilityDenied(CapabilityError):
    """Authorization denied with structured reason."""
    reason: DenialReason
    message: str

class CapabilitySystemError(CapabilityError):
    """Internal capability system error (programming or configuration)."""
```

### Data Classes

```python
@dataclass(frozen=True, slots=True)
class Subject:
    kind: str          # "tool" | "code-run" | "plugin" | "egress-consumer" | ...
    identity: str      # opaque, nonempty, caller-chosen; no secrets

@dataclass(frozen=True, slots=True)
class DirectoryScope:
    root: Path                      # absolute, normalized, no ".." components

@dataclass(frozen=True, slots=True)
class EgressDestination:
    scheme: str; host: str; port: int   # exact-match triple; port in 1..65535

@dataclass(frozen=True, slots=True)
class EgressScope:
    destinations: tuple[EgressDestination, ...]
    credential_reference: str | None

@dataclass(frozen=True, slots=True)
class GrantRequest:
    subject: Subject
    kind: str
    scope: DirectoryScope | EgressScope | None
    ttl_seconds: float              # REQUIRED: finite positive number (deadline)
    max_effects: int                # REQUIRED: positive integer (unit budget)

@dataclass(frozen=True, slots=True)
class AuditEntry:
    seq: int
    timestamp: datetime
    grant_id: str
    subject_kind: str
    subject_identity: str
    kind: str
    reason: str  # "OK" or denial reason string

@dataclass(slots=True)
class CapabilityGrant:
    grant_id: str
    subject: Subject
    kind: str
    scope: DirectoryScope | EgressScope | None
    issued_at: datetime
    deadline: float                 # REQUIRED: no None
    remaining_effects: int          # REQUIRED: no None
    status: GrantStatus
    expires_in(clock): float        # time remaining until deadline (>= 0)
```

### Status Enum

```python
class GrantStatus(str, Enum):
    FORWARD = "FORWARD"    # Usable
    CLEANUP = "CLEANUP"    # Revocation in progress (in-flight effects exist)
    RELEASED = "RELEASED"  # Early surrender (no in-flight effects)
    EXPIRED = "EXPIRED"    # Terminal (deadline passed)
    EXHAUSTED = "EXHAUSTED"  # Terminal (budget exhausted)
    REVOKED = "REVOKED"    # Terminal (revoked)
```

### Denial Reasons

```python
class DenialReason(str, Enum):
    NO_GRANT = "NO_GRANT"         # Grant not issued by this authority
    UNKNOWN_KIND = "UNKNOWN_KIND" # Unregistered kind
    NOT_FORWARD = "NOT_FORWARD"   # RELEASED/CLEANUP/REVOKED
    EXPIRED = "EXPIRED"           # Deadline passed
    EXHAUSTED = "EXHAUSTED"       # Budget exhausted
    SCOPE_MISMATCH = "SCOPE_MISMATCH"  # Path outside scope / destination not allowed
    INVALID_UNITS = "INVALID_UNITS"    # Units < 1 or > remaining
    AUTHORITY_FORKED = "AUTHORITY_FORKED"  # os.getpid() changed
```

### Authority Methods

```python
class CapabilityAuthority:
    def __init__(self, *, clock: Clock = SystemClock(), audit_capacity: int = 1024,
                 extra_kinds: tuple[CapabilityKind, ...] = ()) -> None:
        """
        Initialize a new authority instance.

        extra_kinds: additional capability kinds to register per this authority
                     (name shape + control-char rules, duplicate across built-ins
                     and extras rejected, built-in override rejected).
        """

    def grant(self, request: GrantRequest) -> CapabilityGrant:
        """Issue a new grant from a request (explicit issuance; deny-by-default world)."""

    def authorize(self, grant: CapabilityGrant, *, units: int = 1,
                  target: Path | EgressDestination | None = None) -> None:
        """
        Check + consume budget; raises CapabilityDenied.

        For DirectoryScope grants, target may be None (whole-scope effect) or a Path.
        For EgressScope grants, target is REQUIRED and must match an allowed destination.

        Scope validation happens under the same lock, fail-closed, audit-logged.
        """

    def effect(self, grant: CapabilityGrant, *, units: int = 1,
               target: Path | EgressDestination | None = None) -> ContextManager[None]:
        """
        Authorize + in-flight retention through settlement.

        Scope validation happens under the same lock, fail-closed, audit-logged.

        Note: TOCTOU residual — symlink swapped after the decision is the
        consumer's OS-level RESOLVE_BENEATH obligation.
        """

    def renew(self, grant: CapabilityGrant, *, extend_ttl_seconds: float,
              add_effects: int) -> None:
        """Grant extension semantics (FORWARD only, deadline must be in future)."""

    def release(self, grant: CapabilityGrant) -> None:
        """Early surrender of a grant."""

    def revoke(self, grant: CapabilityGrant, reason: str) -> None:
        """Immediate revocation with reason."""

    def inventory(self) -> tuple[CapabilityGrant, ...]:
        """
        Return live (non-terminal) grants.

        EXPIRED/EXHAUSTED are recorded lazily on check time.
        """

    def audit_trail(self) -> tuple[AuditEntry, ...]:
        """Return the bounded append-only decision log."""
```

### Built-in Kind Constants

```python
WORKSPACE_READ = "workspace-read"
WORKSPACE_WRITE = "workspace-write"
PROCESS_RUN = "process-run"
GIT_INSPECT = "git-inspect"
GIT_MUTATE = "git-mutate"
EGRESS = "egress"
```

## Usage Examples

### Basic Grant and Authorize

```python
from bots5.core.capabilities import (
    CapabilityAuthority,
    DirectoryScope,
    GrantRequest,
    Subject,
    WORKSPACE_READ,
)

authority = CapabilityAuthority()

subject = Subject(kind="tool", identity="my-tool")
request = GrantRequest(
    subject=subject,
    kind=WORKSPACE_READ,
    scope=DirectoryScope(root=Path("/bounded/workspace")),
    ttl_seconds=3600.0,      # REQUIRED: finite positive number
    max_effects=100,         # REQUIRED: positive integer
)
grant = authority.grant(request)

try:
    authority.authorize(grant, units=1)
    # Perform actual work (consumer enforces at syscalls)
except CapabilityDenied as e:
    # Deny operation, return appropriate error to caller
    print(f"Access denied: {e.reason}")
```

### Effect Context Manager

```python
with authority.effect(grant, units=1):
    # Perform actual work; effect is tracked through settlement
    # If exception occurs, effect count is still decremented
    perform_work()
```

### Renewing a Grant

```python
authority.renew(grant, extend_ttl_seconds=1800.0, add_effects=50)
```

### Revoking a Grant

```python
authority.revoke(grant, "user explicitly revoked")
```

### Audit Trail

```python
for entry in authority.audit_trail():
    print(f"{entry.timestamp}: {entry.subject.identity} - {entry.kind} - {entry.reason}")
```

### Per-Authority Extra Kinds

```python
from bots5.core.capabilities import CapabilityAuthority, CapabilityKind

# Define custom kind
custom_kind = CapabilityKind(
    name="custom-effect",
    description="A custom capability kind for my application"
)

# Authority with extra kind
authority = CapabilityAuthority(extra_kinds=(custom_kind,))

# This authority can now grant "custom-effect"
# Other authority instances do NOT see this kind
```

## Thread Safety

The authority uses one internal lock; checks-and-decrements are atomic. No waiting/condition — denial is immediate.

## Testing

See `tests/test_capability_seam.py` for comprehensive tests covering:
- Default-deny world
- Explicit grant and authorize
- TTL with fake clock
- Renew semantics
- Revoke and in-flight effects
- Scope validation
- Kind separation
- Egress semantics
- Plugin no-ambient-trust
- Restart/fork simulation
- Per-authority extra_kinds (replaces global registry)
- Audit logging
- Concurrency
- Inventory
- Subject/scope validation
- Target/scope decision logic
- Deferred settlement (RELEASED vs REVOKED)
- Lazy EXPIRED/EXHAUSTED recording
- expires_in() method

## Residual Risks

**Symlink / TOCTOU**: The scope check (directory or egress destination) happens under lock, but the actual filesystem or network operation happens after the decision. A symlink swap or redirect after the decision is the consumer's OS-level RESOLVE_BENEATH obligation.

**Egress redirect obligation**: The consumer holds grants for destinations actually used; the seam validates destination match but cannot prevent the consumer from connecting to a different endpoint. This is a protocol-level obligation, not a technical enforcement.

**Issuance anchor**: `issued_at` is a wall-clock timestamp for evidence only; it is NOT the expiry authority — `deadline` is the authoritative expiry.
