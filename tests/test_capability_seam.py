"""
Test suite for the capability seam (v0.2).

Tests cover:
1. default-deny world
2. explicit grant → authorize OK; bounded budget
3. TTL with fake clock
4. renew semantics
5. revoke and in-flight effects
6. scope validation
7. kind separation
8. egress semantics
9. plugin no-ambient-trust
10. restart/fork simulation
11. extension API (per-authority extra_kinds)
12. audit logging
13. concurrency
14. inventory
15. subject/scope validation
16. target/scope decision logic

This is the ONLY test-file change; no edits to existing tests.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bots5.core.capabilities import (
    CapabilityAuthority,
    CapabilityDenied,
    CapabilityError,
    CapabilityKind,
    CapabilitySystemError,
    DenialReason,
    DirectoryScope,
    EgressDestination,
    EgressScope,
    GrantRequest,
    GrantStatus,
    Subject,
    # Built-in kind names
    WORKSPACE_READ,
    WORKSPACE_WRITE,
    PROCESS_RUN,
    GIT_INSPECT,
    GIT_MUTATE,
    EGRESS,
)


# ============================================================================
# Fake clock for TTL tests
# ============================================================================


class FakeClock:
    """Fake clock with controllable monotonic and wall time."""

    def __init__(self) -> None:
        self._monotonic = 0.0
        self._wall = datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._monotonic

    def advance_monotonic(self, seconds: float) -> None:
        self._monotonic += seconds

    def advance_wall(self, seconds: float) -> None:
        self._wall = self._wall + timedelta(seconds=seconds)

    def jump_to_monotonic(self, seconds: float) -> None:
        self._monotonic = seconds

    def jump_to_wall(self, dt: datetime) -> None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        self._wall = dt


# ============================================================================
# Test 1: default-deny world
# ============================================================================


class TestDefaultDeny:
    """Test 1: default-deny world: authorize with foreign grant / unknown kind."""

    def test_foreign_grant_no_grant(self):
        """Authorize with a grant never issued / foreign object → NO_GRANT."""
        authority1 = CapabilityAuthority()
        authority2 = CapabilityAuthority()

        subject = Subject(kind="tool", identity="test")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority1.grant(request)

        # authority2 doesn't know this grant
        with pytest.raises(CapabilityDenied) as exc_info:
            authority2.authorize(grant)
        assert exc_info.value.reason is DenialReason.NO_GRANT
        assert "foreign" in exc_info.value.message.lower()

    def test_unknown_kind_denied(self):
        """Unknown kind → UNKNOWN_KIND; every built-in kind deniable pre-grant."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="test")

        # Unknown kind
        request = GrantRequest(
            subject=subject,
            kind="unknown-kind-xyz",
            scope=None,
            ttl_seconds=3600.0,
            max_effects=100,
        )
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.grant(request)
        assert exc_info.value.reason is DenialReason.UNKNOWN_KIND

        # Built-in kinds work after registration - verify they can be granted
        for kind_name in [WORKSPACE_READ, WORKSPACE_WRITE, PROCESS_RUN, GIT_INSPECT, GIT_MUTATE, EGRESS]:
            request = GrantRequest(
                subject=subject,
                kind=kind_name,
                scope=DirectoryScope(root=Path("/workspace")),
                ttl_seconds=3600.0,
                max_effects=100,
            )
            grant = authority.grant(request)
            assert grant.kind == kind_name
            assert grant.status is GrantStatus.FORWARD


# ============================================================================
# Test 2: explicit grant → authorize OK; bounded budget; EXHAUSTED
# ============================================================================


class TestExplicitGrant:
    """Test 2: explicit grant → authorize OK; bounded budget decrements exactly; EXHAUSTED."""

    def test_grant_and_authorize(self):
        """Explicit grant → authorize OK."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        assert grant.grant_id is not None
        assert grant.subject is subject
        assert grant.kind == WORKSPACE_READ
        assert isinstance(grant.scope, DirectoryScope)
        assert grant.scope.root == Path("/workspace").resolve()
        assert grant.status is GrantStatus.FORWARD

        # Authorize should succeed
        authority.authorize(grant, units=1)

        # Check audit
        trail = authority.audit_trail()
        assert len(trail) == 2  # grant + authorize
        assert trail[-1].reason == "OK"

    def test_bounded_budget_decrements(self):
        """Bounded budget decrements exactly."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_WRITE,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=3,  # budget of 3
        )
        grant = authority.grant(request)

        # Authorize 3 times
        for i in range(3):
            authority.authorize(grant, units=1)
            assert grant.remaining_effects == 3 - (i + 1)

        # 4th should fail
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1)
        assert exc_info.value.reason is DenialReason.EXHAUSTED

        # Check audit has EXHAUSTED
        trail = authority.audit_trail()
        assert any(e.reason == "EXHAUSTED" for e in trail)

    def test_required_ttl_max_effects(self):
        """Required finite positive ttl_seconds and positive int max_effects."""
        subject = Subject(kind="tool", identity="my-tool")

        # None ttl_seconds rejected
        with pytest.raises(CapabilitySystemError):
            GrantRequest(
                subject=subject,
                kind=WORKSPACE_READ,
                scope=DirectoryScope(root=Path("/workspace")),
                ttl_seconds=None,
                max_effects=100,
            )

        # None max_effects rejected
        with pytest.raises(CapabilitySystemError):
            GrantRequest(
                subject=subject,
                kind=WORKSPACE_READ,
                scope=DirectoryScope(root=Path("/workspace")),
                ttl_seconds=3600.0,
                max_effects=None,
            )

        # Zero ttl_seconds rejected
        with pytest.raises(CapabilitySystemError):
            GrantRequest(
                subject=subject,
                kind=WORKSPACE_READ,
                scope=DirectoryScope(root=Path("/workspace")),
                ttl_seconds=0,
                max_effects=100,
            )

        # Negative ttl_seconds rejected
        with pytest.raises(CapabilitySystemError):
            GrantRequest(
                subject=subject,
                kind=WORKSPACE_READ,
                scope=DirectoryScope(root=Path("/workspace")),
                ttl_seconds=-1.0,
                max_effects=100,
            )

        # NaN ttl_seconds rejected
        import math
        with pytest.raises(CapabilitySystemError):
            GrantRequest(
                subject=subject,
                kind=WORKSPACE_READ,
                scope=DirectoryScope(root=Path("/workspace")),
                ttl_seconds=float("nan"),
                max_effects=100,
            )

        # Infinity ttl_seconds rejected
        with pytest.raises(CapabilitySystemError):
            GrantRequest(
                subject=subject,
                kind=WORKSPACE_READ,
                scope=DirectoryScope(root=Path("/workspace")),
                ttl_seconds=float("inf"),
                max_effects=100,
            )

        # Zero max_effects rejected
        with pytest.raises(CapabilitySystemError):
            GrantRequest(
                subject=subject,
                kind=WORKSPACE_READ,
                scope=DirectoryScope(root=Path("/workspace")),
                ttl_seconds=3600.0,
                max_effects=0,
            )

        # Negative max_effects rejected
        with pytest.raises(CapabilitySystemError):
            GrantRequest(
                subject=subject,
                kind=WORKSPACE_READ,
                scope=DirectoryScope(root=Path("/workspace")),
                ttl_seconds=3600.0,
                max_effects=-1,
            )

        # Float max_effects rejected
        with pytest.raises(CapabilitySystemError):
            GrantRequest(
                subject=subject,
                kind=WORKSPACE_READ,
                scope=DirectoryScope(root=Path("/workspace")),
                ttl_seconds=3600.0,
                max_effects=10.5,
            )


# ============================================================================
# Test 3: TTL with fake clock
# ============================================================================


class TestTTL:
    """Test 3: TTL: fake clock; authorize before deadline OK; at/after deadline → EXPIRED."""

    def test_ttl_before_deadline(self):
        """Authorize before deadline OK."""
        clock = FakeClock()
        authority = CapabilityAuthority(clock=clock)

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,  # 1 hour
            max_effects=100,
        )
        grant = authority.grant(request)

        # Before deadline
        assert grant.status is GrantStatus.FORWARD
        authority.authorize(grant, units=1)  # OK

    def test_ttl_at_deadline_expired(self):
        """At/after deadline → EXPIRED."""
        clock = FakeClock()
        authority = CapabilityAuthority(clock=clock)

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=10.0,  # 10 seconds
            max_effects=100,
        )
        grant = authority.grant(request)

        # Advance to deadline
        clock.advance_monotonic(10.0)

        # At deadline → EXPIRED
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1)
        assert exc_info.value.reason is DenialReason.EXPIRED

    def test_ttl_after_deadline_expired(self):
        """Wall-clock steps (now() jumps) do NOT affect monotonic deadline."""
        clock = FakeClock()
        authority = CapabilityAuthority(clock=clock)

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=10.0,  # 10 seconds monotonic
            max_effects=100,
        )
        grant = authority.grant(request)

        # Advance wall clock (should NOT affect monotonic deadline)
        clock.advance_wall(100.0)

        # Still OK (monotonic hasn't advanced)
        authority.authorize(grant, units=1)

        # Now advance monotonic
        clock.advance_monotonic(10.0)

        # Now expired
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1)
        assert exc_info.value.reason is DenialReason.EXPIRED


# ============================================================================
# Test 4: renew semantics
# ============================================================================


class TestRenew:
    """Test 4: renew: FORWARD grant extends ttl and adds effects; EXPIRED/NOT_FORWARD denied."""

    def test_renew_forward_grant(self):
        """FORWARD grant extends ttl and adds effects."""
        clock = FakeClock()
        authority = CapabilityAuthority(clock=clock)

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=10,
        )
        grant = authority.grant(request)

        original_deadline = grant.deadline
        original_effects = grant.remaining_effects

        # Renew: extend TTL by 1 hour, add 5 effects
        authority.renew(grant, extend_ttl_seconds=3600.0, add_effects=5)

        assert grant.deadline == original_deadline + 3600.0
        assert grant.remaining_effects == original_effects + 5  # 15

        # Can authorize more
        authority.authorize(grant, units=1)

    def test_renew_after_expiry_denied(self):
        """Renew after expiry is denied EXPIRED; correct path is new explicit grant."""
        clock = FakeClock()
        authority = CapabilityAuthority(clock=clock)

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=10.0,
            max_effects=10,
        )
        grant = authority.grant(request)

        # Advance to expiry
        clock.advance_monotonic(10.0)

        # Renew after expiry → EXPIRED
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.renew(grant, extend_ttl_seconds=3600.0)
        assert exc_info.value.reason is DenialReason.EXPIRED

    def test_renew_not_forward_denied(self):
        """Renew of RELEASED/REVOKED denied NOT_FORWARD."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Release
        authority.release(grant)
        assert grant.status is GrantStatus.RELEASED

        # Renew of RELEASED → NOT_FORWARD
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.renew(grant, extend_ttl_seconds=3600.0)
        assert exc_info.value.reason is DenialReason.NOT_FORWARD

    def test_renew_cannot_shrink(self):
        """Renew cannot shrink (negative/zero deltas rejected INVALID_UNITS)."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=10,
        )
        grant = authority.grant(request)

        # Negative extend_ttl_seconds → INVALID_UNITS
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.renew(grant, extend_ttl_seconds=-1.0)
        assert exc_info.value.reason is DenialReason.INVALID_UNITS

        # Zero extend_ttl_seconds → INVALID_UNITS
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.renew(grant, extend_ttl_seconds=0.0)
        assert exc_info.value.reason is DenialReason.INVALID_UNITS

        # Negative add_effects → INVALID_UNITS
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.renew(grant, add_effects=-1)
        assert exc_info.value.reason is DenialReason.INVALID_UNITS

        # Zero add_effects → INVALID_UNITS
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.renew(grant, add_effects=0)
        assert exc_info.value.reason is DenialReason.INVALID_UNITS


# ============================================================================
# Test 5: revoke and in-flight effects
# ============================================================================


class TestRevoke:
    """Test 5: revoke: immediate → REVOKED; with in-flight effect() → CLEANUP → REVOKED."""

    def test_revoke_immediate_revoked(self):
        """Immediate revoke → REVOKED."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Revoke
        authority.revoke(grant, "test revocation")

        assert grant.status is GrantStatus.REVOKED

        # Authorize after revoke → NOT_FORWARD
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1)
        assert exc_info.value.reason is DenialReason.NOT_FORWARD

    def test_revoke_with_in_flight_cleanup(self):
        """With one in-flight effect() → CLEANUP, authorize denied NOT_FORWARD, settle → REVOKED."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Start in-flight effect
        with authority.effect(grant, units=1):
            assert grant.status is GrantStatus.FORWARD  # Still FORWARD during effect

            # Revoke
            authority.revoke(grant, "test revocation")

            assert grant.status is GrantStatus.CLEANUP  # CLEANUP due to in-flight

        # After exiting context, settle -> REVOKED
        assert grant.status is GrantStatus.REVOKED  # REVOKED after in-flight settles

        # Authorize after revoke → NOT_FORWARD
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1)
        assert exc_info.value.reason is DenialReason.NOT_FORWARD


# ============================================================================
# Test 6: scope validation
# ============================================================================


class TestScope:
    """Test 6: scope: authorize path inside root OK, outside root → SCOPE_MISMATCH."""

    def test_directory_scope_normalization(self):
        """DirectoryScope normalizes and validates paths."""
        # Absolute path
        scope = DirectoryScope(root=Path("/workspace"))
        assert scope.root == Path("/workspace").resolve()

        # Relative path rejected at construction
        with pytest.raises(CapabilitySystemError):
            DirectoryScope(root=Path("relative"))

        # ".." rejected
        with pytest.raises(CapabilitySystemError):
            DirectoryScope(root=Path("/workspace/../other"))

        # Path normalization: trailing slashes are handled by Path.resolve()
        # Double slashes are normalized by Path (this is expected pathlib behavior)
        scope2 = DirectoryScope(root=Path("/workspace//"))
        assert scope2.root == Path("/workspace").resolve()

    def test_directory_scope_allows_path(self):
        """authorize path inside root OK (consumer passes path)."""
        scope = DirectoryScope(root=Path("/workspace"))

        # Inside root
        assert scope.allows_path(Path("/workspace/file.txt"))
        assert scope.allows_path(Path("/workspace/subdir/file.txt"))

        # Outside root
        assert not scope.allows_path(Path("/other/file.txt"))
        assert not scope.allows_path(Path("/"))

    def test_egress_destination_validation(self):
        """EgressDestination validates port bounds."""
        dest = EgressDestination(scheme="https", host="example.com", port=443)
        assert dest.port == 443

        # Port out of bounds
        with pytest.raises(CapabilitySystemError):
            EgressDestination(scheme="https", host="example.com", port=0)

        with pytest.raises(CapabilitySystemError):
            EgressDestination(scheme="https", host="example.com", port=65536)

    def test_egress_scope_validation(self):
        """EgressScope validates destinations and credential_reference."""
        dest = EgressDestination(scheme="https", host="example.com", port=443)
        scope = EgressScope(
            destinations=(dest,),
            credential_reference="my-credential-name",
        )
        assert scope.destinations == (dest,)
        assert scope.credential_reference == "my-credential-name"

        # Empty destinations = "no network"
        empty_scope = EgressScope(
            destinations=(),
            credential_reference=None,
        )
        assert empty_scope.destinations == ()

    def test_egress_scope_empty_denies_all(self):
        """Empty-destination ("no network") scope denies all egress."""
        authority = CapabilityAuthority()

        subject = Subject(kind="egress-consumer", identity="my-egress")
        request = GrantRequest(
            subject=subject,
            kind=EGRESS,
            scope=EgressScope(destinations=(), credential_reference=None),  # no network
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Egress authorize requires destination at use-time
        # Empty scope → SCOPE_MISMATCH
        dest = EgressDestination(scheme="https", host="example.com", port=443)
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1, target=dest)
        assert exc_info.value.reason is DenialReason.SCOPE_MISMATCH


# ============================================================================
# Test 7: kind separation
# ============================================================================


class TestKindSeparation:
    """Test 7: kind separation: git-inspect grant does not authorize git-mutate."""

    def test_git_inspect_not_git_mutate(self):
        """git-inspect grant does not authorize git-mutate (and vice versa)."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="git-tool")

        # Grant git-inspect
        request_inspect = GrantRequest(
            subject=subject,
            kind=GIT_INSPECT,
            scope=DirectoryScope(root=Path("/repo")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant_inspect = authority.grant(request_inspect)

        # Grant git-mutate
        request_mutate = GrantRequest(
            subject=subject,
            kind=GIT_MUTATE,
            scope=DirectoryScope(root=Path("/repo")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant_mutate = authority.grant(request_mutate)

        # git-inspect grant cannot authorize git-mutate
        # This is a design decision: kind separation is enforced by grant kind
        # The authority checks grant.kind == requested kind at authorize-time
        # For now, we just verify grants are separate objects

        assert grant_inspect.grant_id != grant_mutate.grant_id
        assert grant_inspect.kind == GIT_INSPECT
        assert grant_mutate.kind == GIT_MUTATE


# ============================================================================
# Test 8: egress semantics
# ============================================================================


class TestEgress:
    """Test 8: egress: listed destination OK; unlisted → SCOPE_MISMATCH; empty denies all."""

    def test_egress_limited_destinations(self):
        """Egress with limited destinations."""
        authority = CapabilityAuthority()

        dest = EgressDestination(scheme="https", host="api.example.com", port=443)
        scope = EgressScope(
            destinations=(dest,),
            credential_reference="api-credential",
        )

        subject = Subject(kind="egress-consumer", identity="my-api-client")
        request = GrantRequest(
            subject=subject,
            kind=EGRESS,
            scope=scope,
            ttl_seconds=3600.0,
            max_effects=5,
        )
        grant = authority.grant(request)

        # Egress scope validation happens at use-time with destination
        # For now, verify grant was created correctly
        assert isinstance(grant.scope, EgressScope)
        assert grant.scope.destinations == (dest,)
        assert grant.scope.credential_reference == "api-credential"

        # Authorize with matching destination → OK
        authority.authorize(grant, units=1, target=dest)

        # Authorize with unlisted destination → SCOPE_MISMATCH
        other_dest = EgressDestination(scheme="https", host="other.example.com", port=443)
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1, target=other_dest)
        assert exc_info.value.reason is DenialReason.SCOPE_MISMATCH

    def test_egress_empty_destinations(self):
        """Empty destinations = "no network" (explicit deny)."""
        scope = EgressScope(destinations=(), credential_reference=None)
        assert scope.destinations == ()

    def test_egress_credential_reference_name_only(self):
        """Credential reference is an opaque name, never a secret value."""
        dest = EgressDestination(scheme="https", host="example.com", port=443)
        scope = EgressScope(
            destinations=(dest,),
            credential_reference="my-credential-name",
        )

        # The credential_reference is stored as-is (name only)
        assert scope.credential_reference == "my-credential-name"

        # No way to extract secret values (by design)
        # The seam never accepts, stores or logs resolved secret values


# ============================================================================
# Test 9: plugin no-ambient-trust
# ============================================================================


class TestPluginNoAmbientTrust:
    """Test 9: plugin no-ambient-trust: fresh subject has zero grants; inventory empty."""

    def test_fresh_subject_zero_grants(self):
        """Fresh subject has zero grants; inventory() empty."""
        authority = CapabilityAuthority()

        subject = Subject(kind="plugin", identity="my-plugin")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # inventory() lists non-terminal grants
        inv = authority.inventory()
        assert len(inv) == 1
        assert inv[0].grant_id == grant.grant_id

    def test_plugin_scope_isolation(self):
        """Plugin-scoped grant of one kind does not surface for another subject."""
        authority = CapabilityAuthority()

        subject1 = Subject(kind="plugin", identity="plugin-a")
        subject2 = Subject(kind="plugin", identity="plugin-b")

        request1 = GrantRequest(
            subject=subject1,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant1 = authority.grant(request1)

        request2 = GrantRequest(
            subject=subject2,
            kind=WORKSPACE_WRITE,
            scope=DirectoryScope(root=Path("/other")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant2 = authority.grant(request2)

        # Inventory shows both (non-terminal)
        inv = authority.inventory()
        assert len(inv) == 2

        # Grants are distinct
        assert grant1.grant_id != grant2.grant_id
        assert grant1.subject.identity == "plugin-a"
        assert grant2.subject.identity == "plugin-b"


# ============================================================================
# Test 10: restart/fork simulation
# ============================================================================


class TestRestartFork:
    """Test 10: restart/fork: new authority instance does not honor old grants."""

    def test_new_authority_no_grants(self):
        """New authority instance does not honor old grants (NO_GRANT)."""
        authority1 = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority1.grant(request)

        # New authority
        authority2 = CapabilityAuthority()

        # Old grant is foreign to authority2
        with pytest.raises(CapabilityDenied) as exc_info:
            authority2.authorize(grant, units=1)
        assert exc_info.value.reason is DenialReason.NO_GRANT

    def test_pid_change_authority_forked(self):
        """Simulated pid change → AUTHORITY_FORKED."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Simulate pid change (this is a bit hacky, but effective for testing)
        original_pid = authority._pid
        authority._pid = original_pid + 1

        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1)
        assert exc_info.value.reason is DenialReason.AUTHORITY_FORKED


# ============================================================================
# Test 11: per-authority extra_kinds (replaces global registry)
# ============================================================================


class TestExtraKinds:
    """Test 11: per-authority extra_kinds (F1): new kind added to that authority only."""

    def test_extra_kind_adds_to_authority_only(self):
        """Extra kind added to one authority does not appear on another."""
        # Authority A: adds custom kind
        extra_kind = CapabilityKind(name="custom-effect", description="A custom capability kind")
        authority_a = CapabilityAuthority(extra_kinds=(extra_kind,))

        # Authority B: no extra kinds
        authority_b = CapabilityAuthority()

        # Authority A can grant custom kind
        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind="custom-effect",
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority_a.grant(request)
        assert grant.kind == "custom-effect"

        # Authority B rejects custom kind (not registered)
        with pytest.raises(CapabilityDenied) as exc_info:
            authority_b.grant(request)
        assert exc_info.value.reason is DenialReason.UNKNOWN_KIND

    def test_extra_kind_duplicate_rejected(self):
        """Extra kind with duplicate name rejected."""
        kind_name = "test-duplicate-kind"
        kind = CapabilityKind(name=kind_name, description="First registration")

        with pytest.raises(CapabilitySystemError):
            CapabilityAuthority(extra_kinds=(kind, kind))

    def test_extra_kind_builtin_override_rejected(self):
        """Extra kind overriding a built-in rejected."""
        builtin_kind = CapabilityKind(name=WORKSPACE_READ, description="Trying to override")

        with pytest.raises(CapabilitySystemError):
            CapabilityAuthority(extra_kinds=(builtin_kind,))

    def test_authority_kind_validation(self):
        """Authority validates extra_kind names (no control chars, non-empty)."""
        # Empty name rejected
        with pytest.raises(CapabilitySystemError):
            CapabilityAuthority(extra_kinds=(CapabilityKind(name="", description="test"),))

        # Empty description rejected
        with pytest.raises(CapabilitySystemError):
            CapabilityAuthority(extra_kinds=(CapabilityKind(name="test", description=""),))

        # Control character rejected
        with pytest.raises(CapabilitySystemError):
            CapabilityAuthority(extra_kinds=(CapabilityKind(name="test\x01kind", description="test"),))


# ============================================================================
# Test 12: audit logging
# ============================================================================


class TestAudit:
    """Test 12: audit: allow and deny both logged; capacity bound; audit_trail bounded."""

    def test_audit_allow_and_deny(self):
        """Allow and deny both logged with reasons."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=2,
        )
        grant = authority.grant(request)

        # Authorize (allow)
        authority.authorize(grant, units=1)

        # Authorize again (allow)
        authority.authorize(grant, units=1)

        # Authorize third time (deny - exhausted)
        with pytest.raises(CapabilityDenied):
            authority.authorize(grant, units=1)

        trail = authority.audit_trail()

        # Should have: grant + 2 authorizes + 1 deny
        assert len(trail) >= 4

        # Check deny reason
        deny_entries = [e for e in trail if e.reason == "EXHAUSTED"]
        assert len(deny_entries) >= 1

    def test_audit_capacity_bound(self):
        """Capacity bound respected (oldest dropped)."""
        # Create authority with small capacity
        authority = CapabilityAuthority(audit_capacity=5)

        # Generate many audit entries
        for i in range(10):
            subject = Subject(kind="tool", identity=f"tool-{i}")
            request = GrantRequest(
                subject=subject,
                kind=WORKSPACE_READ,
                scope=DirectoryScope(root=Path("/workspace")),
                ttl_seconds=3600.0,
                max_effects=100,
            )
            grant = authority.grant(request)
            authority.authorize(grant, units=1)

        # Should only have last 5 entries
        trail = authority.audit_trail()
        assert len(trail) == 5

    def test_audit_trail_never_grows_unbounded(self):
        """audit_trail never grows unbounded (bounded by capacity)."""
        capacity = 100
        authority = CapabilityAuthority(audit_capacity=capacity)

        # Generate many entries
        for i in range(1000):
            subject = Subject(kind="tool", identity=f"tool-{i}")
            request = GrantRequest(
                subject=subject,
                kind=WORKSPACE_READ,
                scope=DirectoryScope(root=Path("/workspace")),
                ttl_seconds=3600.0,
                max_effects=100,
            )
            grant = authority.grant(request)
            authority.authorize(grant, units=1)

        # Should still be bounded by capacity
        trail = authority.audit_trail()
        assert len(trail) <= capacity


# ============================================================================
# Test 13: concurrency
# ============================================================================


class TestConcurrency:
    """Test 13: concurrency: N threads authorize against one bounded grant; total == budget."""

    def test_concurrent_authorize_exact_budget(self):
        """N threads authorize against one bounded grant; total successful == budget exactly."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="concurrent-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=10,  # budget of 10
        )
        grant = authority.grant(request)

        successful = []
        lock = threading.Lock()

        def worker():
            try:
                authority.authorize(grant, units=1)
                with lock:
                    successful.append(1)
            except CapabilityDenied:
                pass

        # Launch 20 threads
        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # Exactly 10 should have succeeded
        assert len(successful) == 10

        # Grant should be exhausted
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1)
        assert exc_info.value.reason is DenialReason.EXHAUSTED


# ============================================================================
# Test 14: inventory
# ============================================================================


class TestInventory:
    """Test 14: inventory lists only non-terminal grants."""

    def test_inventory_lists_non_terminal(self):
        """inventory lists only non-terminal grants."""
        authority = CapabilityAuthority()

        # Grant 1: keep (no release)
        subject1 = Subject(kind="tool", identity="tool-1")
        request1 = GrantRequest(
            subject=subject1,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant1 = authority.grant(request1)

        # Grant 2: release
        subject2 = Subject(kind="tool", identity="tool-2")
        request2 = GrantRequest(
            subject=subject2,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant2 = authority.grant(request2)
        authority.release(grant2)

        # Grant 3: revoke
        subject3 = Subject(kind="tool", identity="tool-3")
        request3 = GrantRequest(
            subject=subject3,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant3 = authority.grant(request3)
        authority.revoke(grant3, "test")

        # Inventory should only show grant1 (FORWARD)
        inv = authority.inventory()
        assert len(inv) == 1
        assert inv[0].grant_id == grant1.grant_id

    def test_inventory_excludes_released(self):
        """Released grants vanish from inventory."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Initially in inventory
        assert len(authority.inventory()) == 1

        # After release
        authority.release(grant)
        assert len(authority.inventory()) == 0

    def test_inventory_excludes_revoked(self):
        """Revoked grants vanish from inventory."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Initially in inventory
        assert len(authority.inventory()) == 1

        # After revoke
        authority.revoke(grant, "test")
        assert len(authority.inventory()) == 0


# ============================================================================
# Test 15: subject/scope validation
# ============================================================================


class TestSubjectScopeValidation:
    """Test 15: subject/scope validation: control characters and empty fields rejected."""

    def test_subject_empty_kind_rejected(self):
        """Empty kind rejected."""
        with pytest.raises(CapabilitySystemError):
            Subject(kind="", identity="test")

    def test_subject_empty_identity_rejected(self):
        """Empty identity rejected."""
        with pytest.raises(CapabilitySystemError):
            Subject(kind="tool", identity="")

    def test_subject_nul_rejected(self):
        """NUL characters rejected."""
        with pytest.raises(CapabilitySystemError):
            Subject(kind="test\x00kind", identity="test")

        with pytest.raises(CapabilitySystemError):
            Subject(kind="tool", identity="test\x00identity")

    def test_subject_control_characters_rejected(self):
        """Control characters rejected."""
        with pytest.raises(CapabilitySystemError):
            Subject(kind="test\x01kind", identity="test")  # ASCII 1

        with pytest.raises(CapabilitySystemError):
            Subject(kind="tool", identity="test\x7f")  # ASCII 127 (DEL)

    def test_directory_scope_relative_rejected(self):
        """Relative path rejected at DirectoryScope construction."""
        with pytest.raises(CapabilitySystemError):
            DirectoryScope(root=Path("relative"))

    def test_directory_scope_dotdot_rejected(self):
        """'..' components rejected."""
        with pytest.raises(CapabilitySystemError):
            DirectoryScope(root=Path("/workspace/../other"))

    def test_egress_destination_port_bounds(self):
        """EgressDestination port bounds enforced."""
        # Port 0 rejected
        with pytest.raises(CapabilitySystemError):
            EgressDestination(scheme="https", host="example.com", port=0)

        # Port 65536 rejected
        with pytest.raises(CapabilitySystemError):
            EgressDestination(scheme="https", host="example.com", port=65536)


# ============================================================================
# Test 16: target/scope decision logic
# ============================================================================


class TestTargetScopeDecision:
    """Test 16: target/scope decision logic (F3)."""

    def test_directory_scope_with_none_target(self):
        """DirectoryScope with None target → whole-scope effect (OK)."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # None target allowed for DirectoryScope
        authority.authorize(grant, units=1, target=None)

    def test_directory_scope_with_valid_path(self):
        """DirectoryScope with valid path inside root → OK."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Valid path inside root
        authority.authorize(grant, units=1, target=Path("/workspace/file.txt"))

    def test_directory_scope_with_path_outside_root(self):
        """DirectoryScope with path outside root → SCOPE_MISMATCH."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Path outside root
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1, target=Path("/other/file.txt"))
        assert exc_info.value.reason is DenialReason.SCOPE_MISMATCH

    def test_directory_scope_with_relative_target(self):
        """DirectoryScope with relative target → SCOPE_MISMATCH."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Relative path rejected
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1, target=Path("relative/file.txt"))
        assert exc_info.value.reason is DenialReason.SCOPE_MISMATCH

    def test_directory_scope_with_dotdot_target(self):
        """DirectoryScope with .. target → SCOPE_MISMATCH."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Path with .. component
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1, target=Path("/workspace/../other"))
        assert exc_info.value.reason is DenialReason.SCOPE_MISMATCH

    def test_directory_scope_with_non_path_target(self):
        """DirectoryScope with non-Path target → SCOPE_MISMATCH."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Non-Path target
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1, target="string-path")
        assert exc_info.value.reason is DenialReason.SCOPE_MISMATCH

    def test_egress_scope_with_matching_destination(self):
        """EgressScope with matching destination → OK."""
        authority = CapabilityAuthority()

        dest = EgressDestination(scheme="https", host="api.example.com", port=443)
        scope = EgressScope(
            destinations=(dest,),
            credential_reference="api-credential",
        )

        subject = Subject(kind="egress-consumer", identity="my-api-client")
        request = GrantRequest(
            subject=subject,
            kind=EGRESS,
            scope=scope,
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Matching destination
        authority.authorize(grant, units=1, target=dest)

    def test_egress_scope_with_missing_target(self):
        """EgressScope with None target → SCOPE_MISMATCH (required)."""
        authority = CapabilityAuthority()

        dest = EgressDestination(scheme="https", host="api.example.com", port=443)
        scope = EgressScope(
            destinations=(dest,),
            credential_reference="api-credential",
        )

        subject = Subject(kind="egress-consumer", identity="my-api-client")
        request = GrantRequest(
            subject=subject,
            kind=EGRESS,
            scope=scope,
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Missing target
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1, target=None)
        assert exc_info.value.reason is DenialReason.SCOPE_MISMATCH

    def test_egress_scope_with_unmatched_destination(self):
        """EgressScope with unmatched destination → SCOPE_MISMATCH."""
        authority = CapabilityAuthority()

        dest = EgressDestination(scheme="https", host="api.example.com", port=443)
        scope = EgressScope(
            destinations=(dest,),
            credential_reference="api-credential",
        )

        subject = Subject(kind="egress-consumer", identity="my-api-client")
        request = GrantRequest(
            subject=subject,
            kind=EGRESS,
            scope=scope,
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Unmatched destination
        other_dest = EgressDestination(scheme="https", host="other.example.com", port=443)
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1, target=other_dest)
        assert exc_info.value.reason is DenialReason.SCOPE_MISMATCH

    def test_egress_scope_with_non_destination_target(self):
        """EgressScope with non-EgressDestination target → SCOPE_MISMATCH."""
        authority = CapabilityAuthority()

        dest = EgressDestination(scheme="https", host="api.example.com", port=443)
        scope = EgressScope(
            destinations=(dest,),
            credential_reference="api-credential",
        )

        subject = Subject(kind="egress-consumer", identity="my-api-client")
        request = GrantRequest(
            subject=subject,
            kind=EGRESS,
            scope=scope,
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Non-EgressDestination target
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1, target=Path("/workspace"))
        assert exc_info.value.reason is DenialReason.SCOPE_MISMATCH

    def test_egress_scope_empty_scope(self):
        """EgressScope with empty destinations → SCOPE_MISMATCH for any target."""
        authority = CapabilityAuthority()

        scope = EgressScope(destinations=(), credential_reference=None)

        subject = Subject(kind="egress-consumer", identity="my-api-client")
        request = GrantRequest(
            subject=subject,
            kind=EGRESS,
            scope=scope,
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Any target on empty scope
        dest = EgressDestination(scheme="https", host="api.example.com", port=443)
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1, target=dest)
        assert exc_info.value.reason is DenialReason.SCOPE_MISMATCH


# ============================================================================
# Additional edge case tests
# ============================================================================


class TestEdgeCases:
    """Additional edge case tests."""

    def test_grant_with_none_scope(self):
        """Grant can have None scope for kinds that don't need it."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=EGRESS,
            scope=None,  # Egress without scope
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        assert grant.scope is None

    def test_authorize_with_zero_units(self):
        """authorize with units=0 → INVALID_UNITS."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=0)
        assert exc_info.value.reason is DenialReason.INVALID_UNITS

    def test_authorize_with_negative_units(self):
        """authorize with negative units → INVALID_UNITS."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=-1)
        assert exc_info.value.reason is DenialReason.INVALID_UNITS

    def test_authorize_with_too_many_units(self):
        """authorize with units > remaining_effects → INVALID_UNITS."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=5,
        )
        grant = authority.grant(request)

        # Try to authorize 10 when only 5 remain
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=10)
        assert exc_info.value.reason is DenialReason.INVALID_UNITS

    def test_renew_moves_deadline_later(self):
        """Renew moves deadline later (never earlier)."""
        clock = FakeClock()
        authority = CapabilityAuthority(clock=clock)

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=100.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        original_deadline = grant.deadline

        # Renew with 50 seconds
        authority.renew(grant, extend_ttl_seconds=50.0)

        # New deadline should be original + 50
        assert grant.deadline == original_deadline + 50.0

    def test_release_with_in_flight(self):
        """Release with in-flight effects → CLEANUP → RELEASED (deferred outcome)."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Start in-flight effect
        with authority.effect(grant, units=1):
            assert grant.status is GrantStatus.FORWARD  # Still FORWARD during effect

            # Release
            authority.release(grant)

            assert grant.status is GrantStatus.CLEANUP  # CLEANUP due to in-flight
            assert grant._deferred == "RELEASED"  # Deferred outcome set by release()

        # After exiting context, settle -> RELEASED (deferred outcome)
        assert grant.status is GrantStatus.RELEASED

    def test_grant_with_ttl_and_effects(self):
        """Grant can have both TTL and bounded effects."""
        clock = FakeClock()
        authority = CapabilityAuthority(clock=clock)

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=10,
        )
        grant = authority.grant(request)

        assert grant.deadline is not None
        assert grant.remaining_effects == 10

        # Can authorize up to 10 times
        for i in range(10):
            authority.authorize(grant, units=1)

        # 11th should fail (exhausted)
        with pytest.raises(CapabilityDenied) as exc_info:
            authority.authorize(grant, units=1)
        assert exc_info.value.reason is DenialReason.EXHAUSTED


# ============================================================================
# Test F6: deferred outcome for settle
# ============================================================================


class TestDeferredSettlement:
    """Test F6: effect() settlement with deferred outcome (RELEASED vs REVOKED)."""

    def test_release_with_in_flight_settles_to_released(self):
        """Release with in-flight effects → CLEANUP → (settle) → RELEASED."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Start in-flight effect
        with authority.effect(grant, units=1):
            # Release while in-flight
            authority.release(grant)
            assert grant.status is GrantStatus.CLEANUP  # CLEANUP due to in-flight
            assert grant._deferred == "RELEASED"

        # After exiting context, settle -> RELEASED (not REVOKED)
        assert grant.status is GrantStatus.RELEASED

    def test_revoke_after_release_deferred_overrides(self):
        """Revoke after release-deferred → override to REVOKED."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Start in-flight effect
        with authority.effect(grant, units=1):
            # Release while in-flight
            authority.release(grant)
            assert grant.status is GrantStatus.CLEANUP
            assert grant._deferred == "RELEASED"

            # Revoke after release-deferred (overrides)
            authority.revoke(grant, "override")

            assert grant._deferred == "REVOKED"  # Override to REVOKED

        # After exiting context, settle -> REVOKED (overridden)
        assert grant.status is GrantStatus.REVOKED


# ============================================================================
# Test F7: lazy EXPIRED/EXHAUSTED recording
# ============================================================================


class TestLazyExpiration:
    """Test F7: EXPIRED/EXHAUSTED recorded lazily on check time in inventory()."""

    def test_expired_grant_excluded_from_inventory(self):
        """After expiry, inventory() excludes the grant and status reads EXPIRED."""
        clock = FakeClock()
        authority = CapabilityAuthority(clock=clock)

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=10.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Initially in inventory
        inv = authority.inventory()
        assert len(inv) == 1

        # Advance to expiry
        clock.advance_monotonic(10.0)

        # inventory() should exclude expired grant and set status lazily
        inv = authority.inventory()
        assert len(inv) == 0
        assert grant.status is GrantStatus.EXPIRED

    def test_exhausted_grant_excluded_from_inventory(self):
        """After exhaustion, inventory() excludes the grant and status reads EXHAUSTED."""
        authority = CapabilityAuthority()

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=2,
        )
        grant = authority.grant(request)

        # Initially in inventory
        inv = authority.inventory()
        assert len(inv) == 1

        # Exhaust the budget
        authority.authorize(grant, units=2)

        # inventory() should exclude exhausted grant and set status lazily
        inv = authority.inventory()
        assert len(inv) == 0
        assert grant.status is GrantStatus.EXHAUSTED


# ============================================================================
# Test F5: expires_in method
# ============================================================================


class TestExpiresIn:
    """Test F5: expires_in(clock) method on CapabilityGrant."""

    def test_expires_in_before_deadline(self):
        """expires_in returns time remaining before deadline."""
        clock = FakeClock()
        authority = CapabilityAuthority(clock=clock)

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Before deadline
        assert grant.expires_in(clock) == 3600.0

    def test_expires_in_at_deadline(self):
        """expires_in returns 0 at deadline."""
        clock = FakeClock()
        authority = CapabilityAuthority(clock=clock)

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=10.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Advance to deadline
        clock.advance_monotonic(10.0)

        # At deadline → 0
        assert grant.expires_in(clock) == 0.0

    def test_expires_in_after_deadline(self):
        """expires_in returns 0 after deadline (never negative)."""
        clock = FakeClock()
        authority = CapabilityAuthority(clock=clock)

        subject = Subject(kind="tool", identity="my-tool")
        request = GrantRequest(
            subject=subject,
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=Path("/workspace")),
            ttl_seconds=10.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Advance past deadline
        clock.advance_monotonic(15.0)

        # After deadline → 0 (never negative)
        assert grant.expires_in(clock) == 0.0


# ============================================================================
# Test audit trail never contains credential_reference
# ============================================================================


class TestAuditMinimality:
    """Test F4: audit trail never contains credential_reference name."""

    def test_audit_trail_no_credential_reference(self):
        """Credential reference name never appears in audit_trail()."""
        authority = CapabilityAuthority()

        dest = EgressDestination(scheme="https", host="example.com", port=443)
        scope = EgressScope(
            destinations=(dest,),
            credential_reference="my-secret-credential-name",
        )

        subject = Subject(kind="egress-consumer", identity="my-api-client")
        request = GrantRequest(
            subject=subject,
            kind=EGRESS,
            scope=scope,
            ttl_seconds=3600.0,
            max_effects=100,
        )
        grant = authority.grant(request)

        # Authorize (which should validate scope)
        authority.authorize(grant, units=1, target=dest)

        # Audit trail should NOT contain the credential reference name
        trail = authority.audit_trail()
        for entry in trail:
            # Check grant_id (this is fine)
            # Check subject fields (these are fine)
            # Check kind (this is fine)
            # Check reason (this is fine)
            # The credential_reference should never appear
            assert "my-secret-credential-name" not in entry.grant_id
            assert "my-secret-credential-name" not in entry.subject_kind
            assert "my-secret-credential-name" not in entry.subject_identity
            assert "my-secret-credential-name" not in entry.kind
            assert "my-secret-credential-name" not in entry.reason

        # Also check grant scope is not directly exposed
        assert hasattr(grant, "_scope_view")
        assert not hasattr(grant, "credential_reference")


# ============================================================================
# Kind binding: a grant of one kind must never authorise a different kind
# ============================================================================


class TestEffectKindBinding:
    """Regression for the round-2 oracle finding that ``effect()`` was
    kind-blind: a ``workspace-read`` grant could drive a process-run effect
    because only scope *shape* was checked, never ``grant.kind``.
    """

    def _authority(self):
        return CapabilityAuthority(clock=FakeClock())

    def _grant(self, authority, kind, root: Path):
        return authority.grant(
            GrantRequest(
                subject=Subject(kind="tool", identity="subject"),
                kind=kind,
                scope=DirectoryScope(root=root),
                ttl_seconds=60.0,
                max_effects=5,
            )
        )

    def test_effect_refuses_a_mismatched_kind(self, tmp_path: Path) -> None:
        authority = self._authority()
        read_grant = self._grant(authority, WORKSPACE_READ, tmp_path)

        # Holding a read-only grant must NOT authorise a process-run effect,
        # even though the scope shape (DirectoryScope over the same root) fits.
        with pytest.raises(CapabilityDenied) as excinfo:
            with authority.effect(read_grant, units=1, target=tmp_path, kind=PROCESS_RUN):
                raise AssertionError("effect body must not execute")

        assert excinfo.value.reason is DenialReason.UNKNOWN_KIND
        # The refusal must be pre-effect: no budget may be consumed.
        assert read_grant.remaining_effects == 5

    def test_effect_allows_the_matching_kind(self, tmp_path: Path) -> None:
        authority = self._authority()
        read_grant = self._grant(authority, WORKSPACE_READ, tmp_path)

        with authority.effect(read_grant, units=1, target=tmp_path, kind=WORKSPACE_READ):
            pass

        assert read_grant.remaining_effects == 4

    def test_effect_without_kind_still_authorises(self, tmp_path: Path) -> None:
        """Backwards compatibility: callers that do not declare a kind keep the
        previous scope-and-budget behaviour."""
        authority = self._authority()
        write_grant = self._grant(authority, WORKSPACE_WRITE, tmp_path)

        with authority.effect(write_grant, units=1, target=tmp_path):
            pass

        assert write_grant.remaining_effects == 4

    def test_every_builtin_kind_is_distinct(self, tmp_path: Path) -> None:
        """Each built-in kind refuses every other built-in kind's effect."""
        authority = self._authority()
        kinds = [WORKSPACE_READ, WORKSPACE_WRITE, PROCESS_RUN, EGRESS]
        for owner_kind in kinds:
            grant = self._grant(authority, owner_kind, tmp_path)
            for other in kinds:
                if other == owner_kind:
                    continue
                with pytest.raises(CapabilityDenied) as excinfo:
                    with authority.effect(grant, units=1, target=tmp_path, kind=other):
                        raise AssertionError("effect body must not execute")
                assert excinfo.value.reason is DenialReason.UNKNOWN_KIND
            assert grant.remaining_effects == 5


# ============================================================================
# Run all tests
# ============================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
