"""Adversarial regression tests for plugin/shared-authority integration.

These tests verify the two defects fixed under Q2:

1. Every plugin effect that consumes a capability enters the ONE shared
   ``CapabilityAuthority.effect()`` boundary, decrements budget by exactly
   one, and appends exactly one audit entry.
2. Lifecycle revoke/drain/terminal transitions invalidate the corresponding
   shared-authority grant; presenting a previously-issued shared grant after
   revocation is refused.

They are intentionally written to FAIL against the pre-fix code and PASS
after the fix.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bots5.core.capabilities import (
    CapabilityAuthority,
    CapabilityDenied,
    DenialReason,
    DirectoryScope,
    GrantStatus,
)
from bots5.plugins import (
    Capability,
    PluginCapabilityDenied,
    PluginHost,
    PluginManifest,
)


def _manifest(plugin_id: str = "bots5.plugin.a") -> PluginManifest:
    return PluginManifest(plugin_id=plugin_id, semantic_version="0.1.0")


class FakeClock:
    """Minimal fake clock for expiry tests."""

    def __init__(self) -> None:
        self._mono = 0.0
        self._wall = datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self._wall += timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# effect spending
# ---------------------------------------------------------------------------


def test_plugin_read_decrements_shared_budget_and_adds_audit_entry(tmp_path: Path):
    """A plugin workspace read spends exactly one unit on the shared authority."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    authority = CapabilityAuthority()
    host = PluginHost(capability_authority=authority, roots={"ws": workspace})

    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.bind_grants("bots5.plugin.a", {Capability.WORKSPACE_READ})
    host.admit("bots5.plugin.a")
    ctx = host.activate("bots5.plugin.a")

    grant_id = host._authority_grants["bots5.plugin.a"]["workspace-read"]
    grant = authority._grants[grant_id]
    before_budget = grant.remaining_effects
    before_audit = len(authority.audit_trail())

    result = ctx.facade.query().read_text("ws", "a.txt")
    assert result == "one\ntwo\nthree\n"

    assert grant.remaining_effects == before_budget - 1
    assert len(authority.audit_trail()) == before_audit + 1
    assert authority.audit_trail()[-1].reason == "OK"
    assert authority.audit_trail()[-1].kind == "workspace-read"


def test_plugin_named_effect_decrements_shared_budget(tmp_path: Path):
    """A plugin named effect spends exactly one unit on the shared authority."""
    authority = CapabilityAuthority()
    host = PluginHost(capability_authority=authority)

    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.bind_grants("bots5.plugin.a", {Capability.NAMED_EFFECT_APPEND})
    host.admit("bots5.plugin.a")
    ctx = host.activate("bots5.plugin.a")

    grant_id = host._authority_grants["bots5.plugin.a"]["workspace-write"]
    grant = authority._grants[grant_id]
    before_budget = grant.remaining_effects
    before_audit = len(authority.audit_trail())

    receipt = ctx.facade.append_named_effect("ping", {"x": 1})
    assert receipt.outcome == "applied"

    assert grant.remaining_effects == before_budget - 1
    assert len(authority.audit_trail()) == before_audit + 1
    assert authority.audit_trail()[-1].kind == "workspace-write"


def test_plugin_extension_state_decrements_shared_budget(tmp_path: Path):
    """Each extension-state operation spends one unit on the shared authority."""
    authority = CapabilityAuthority()
    host = PluginHost(capability_authority=authority)

    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.bind_grants("bots5.plugin.a", {Capability.EXTENSION_STATE})
    host.admit("bots5.plugin.a")
    ctx = host.activate("bots5.plugin.a")

    grant_id = host._authority_grants["bots5.plugin.a"]["workspace-write"]
    grant = authority._grants[grant_id]

    ctx.facade.extension_state().set("k", "v")
    assert grant.remaining_effects == 1000 - 1
    ctx.facade.extension_state().get("k")
    assert grant.remaining_effects == 1000 - 2


# ---------------------------------------------------------------------------
# revocation invalidates the shared grant
# ---------------------------------------------------------------------------


def test_revoke_invalidates_shared_grant(tmp_path: Path):
    """After lifecycle revoke, the previously-issued shared grant is refused."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")

    authority = CapabilityAuthority()
    host = PluginHost(capability_authority=authority, roots={"ws": workspace})

    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.bind_grants("bots5.plugin.a", {Capability.WORKSPACE_READ})
    host.admit("bots5.plugin.a")
    ctx = host.activate("bots5.plugin.a")

    # Capture the shared grant object.
    grant_id = host._authority_grants["bots5.plugin.a"]["workspace-read"]
    captured_grant = authority._grants[grant_id]
    before_budget = captured_grant.remaining_effects

    # Plugin effect works before revoke.
    assert ctx.facade.query().read_text("ws", "a.txt") == "x\n"

    host.revoke("bots5.plugin.a")

    # Presenting the previously-issued shared grant directly is refused.
    with pytest.raises(CapabilityDenied) as excinfo:
        with authority.effect(captured_grant, units=1, kind="workspace-read"):
            pass
    assert excinfo.value.reason is DenialReason.NOT_FORWARD
    # No budget was consumed by the refused presentation.
    assert captured_grant.remaining_effects == before_budget - 1  # only the successful read

    # The plugin-facing seam is also dead.
    with pytest.raises(PluginCapabilityDenied, match="no longer live"):
        ctx.facade.query().read_text("ws", "a.txt")


# ---------------------------------------------------------------------------
# exhausted / expired shared grant is refused
# ---------------------------------------------------------------------------


def test_exhausted_shared_grant_refuses_plugin_effect(tmp_path: Path):
    """When the shared grant is exhausted, the plugin seam refuses with the authority reason."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for i in range(3):
        (workspace / f"{i}.txt").write_text("x\n", encoding="utf-8")

    authority = CapabilityAuthority()
    host = PluginHost(capability_authority=authority, roots={"ws": workspace})

    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.bind_grants("bots5.plugin.a", {Capability.WORKSPACE_READ})
    host.admit("bots5.plugin.a")
    ctx = host.activate("bots5.plugin.a")

    # Exhaust the workspace-read grant (bind_grants issues max_effects=1000).
    view = ctx.facade.query()
    for i in range(1000):
        view.read_text("ws", "0.txt")

    grant_id = host._authority_grants["bots5.plugin.a"]["workspace-read"]
    grant = authority._grants[grant_id]
    # The last successful consumption leaves the grant FORWARD with zero remaining;
    # the next attempted effect records the EXHAUSTED state.
    assert grant.remaining_effects == 0

    # The next plugin effect is refused with the seam's denial reason.
    with pytest.raises(PluginCapabilityDenied) as excinfo:
        view.read_text("ws", "0.txt")
    assert "EXHAUSTED" in str(excinfo.value) or "budget" in str(excinfo.value).lower()
    assert grant.status is GrantStatus.EXHAUSTED


def test_expired_shared_grant_refuses_plugin_effect(tmp_path: Path):
    """When the shared grant expires, the plugin seam refuses."""
    clock = FakeClock()
    authority = CapabilityAuthority(clock=clock)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    host = PluginHost(capability_authority=authority, roots={"ws": workspace})

    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.bind_grants("bots5.plugin.a", {Capability.WORKSPACE_READ})
    host.admit("bots5.plugin.a")
    ctx = host.activate("bots5.plugin.a")

    # Advance past the 3600s TTL issued by bind_grants.
    clock.advance(3601.0)

    with pytest.raises(PluginCapabilityDenied) as excinfo:
        ctx.facade.query().read_text("ws", "a.txt")
    assert "EXPIRED" in str(excinfo.value) or "deadline" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# kind binding through the plugin path
# ---------------------------------------------------------------------------


def test_plugin_cannot_reach_effect_of_unheld_kind(tmp_path: Path):
    """A plugin holding only a workspace-read grant cannot perform a workspace-write effect."""
    authority = CapabilityAuthority()
    host = PluginHost(capability_authority=authority)

    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.bind_grants("bots5.plugin.a", {Capability.WORKSPACE_READ})
    host.admit("bots5.plugin.a")
    ctx = host.activate("bots5.plugin.a")

    # GrantSet refuses before any authority interaction.
    with pytest.raises(PluginCapabilityDenied, match="named_effect.append"):
        ctx.facade.append_named_effect("nope", {})


# ---------------------------------------------------------------------------
# repeated revoke idempotency
# ---------------------------------------------------------------------------


def test_repeated_authority_revoke_is_idempotent(tmp_path: Path):
    """Revoking an already-revoked shared grant does not resurrect it."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")

    authority = CapabilityAuthority()
    host = PluginHost(capability_authority=authority, roots={"ws": workspace})

    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.bind_grants("bots5.plugin.a", {Capability.WORKSPACE_READ})
    host.admit("bots5.plugin.a")
    host.activate("bots5.plugin.a")

    grant_id = host._authority_grants["bots5.plugin.a"]["workspace-read"]
    grant = authority._grants[grant_id]

    host.revoke("bots5.plugin.a")
    assert grant.status is GrantStatus.REVOKED

    # Repeated authority-level revoke must stay a no-op, not resurrect.
    authority.revoke(grant, "second revoke")
    assert grant.status is GrantStatus.REVOKED
    assert grant.grant_id not in {g.grant_id for g in authority.inventory()}
