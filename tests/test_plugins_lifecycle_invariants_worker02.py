"""T0/T1 — WORKER-02 (plugin-lifecycle) independent verification suite.

Distinctly named so it never clobbers the api-manifest sibling's
``test_plugins_lifecycle.py`` / ``test_plugins_capability_denial.py`` /
``test_plugins_manifest_api.py``.  This file verifies and pins down the
hardening added after independent review:

* drain-before-terminal on EVERY path to a non-operational state
  (disable/purge no longer skip DRAINING from ADMITTED/ACTIVE);
* activation drives the full documented sequence — GRANT_REQUESTED is
  never bypassed when capabilities were declared;
* monotonic terminality survives convenience paths: REVOKED/PURGED cannot
  be revived as a side effect of ``activate()``;
* stale surfaces (facade, query view, state view, grant view) fail closed
  after drain/disable/revoke — no GC-dependent correctness;
* re-validation after DISABLED/REVOKED re-parses the manifest declaration
  and clears grants before any new activation;
* the explicit event-subscription seam requires an ACTIVE plugin with an
  EVENT_SUBSCRIBE grant and closes subscriptions through the handle.
"""

from __future__ import annotations

import pytest

from bots5.plugins import (
    Capability,
    LifecycleState,
    PluginCapabilityDenied,
    PluginHost,
    PluginLifecycleError,
    PluginManifest,
    TERMINAL_STATES,
)

A = "bots5.plugin.a"


def _manifest(plugin_id: str = A, **kw) -> PluginManifest:
    return PluginManifest(plugin_id=plugin_id, semantic_version="0.1.0", **kw)


class _Sub:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _bring_up(host, plugin_id=A, capabilities=frozenset()):
    host.discover(_manifest(plugin_id))
    host.validate(plugin_id)
    if capabilities or host.state_of(plugin_id) is not None:
        host.bind_grants(plugin_id, set(capabilities))
    host.admit(plugin_id)
    return host.activate(plugin_id)


# -- drain-before-terminal on every path ------------------------------------

def test_disable_from_active_drains_subscriptions_first():
    """DEFECT V1: disable() used to jump ACTIVE -> DISABLED directly."""
    host = PluginHost()
    ctx = _bring_up(host, capabilities={Capability.NAMED_EFFECT_APPEND})
    handle = host.handle(A)
    sub = _Sub()
    handle.register_subscription(sub)
    host.disable(A)
    assert sub.closed is True, "subscription must be closed before DISABLED"
    assert handle.state is LifecycleState.DISABLED


def test_disable_from_admitted_drains_first():
    host = PluginHost()
    host.discover(_manifest())
    host.validate(A)
    host.bind_grants(A, {Capability.EVENT_SUBSCRIBE})
    host.admit(A)
    handle = host.handle(A)
    sub = _Sub()
    handle.register_subscription(sub)
    host.disable(A)
    assert sub.closed is True


def test_purge_from_active_drains_and_disables_first():
    """DEFECT V1 (purge variant): purge() could leave a live ACTIVE handle."""
    host = PluginHost()
    ctx = _bring_up(host, capabilities={Capability.NAMED_EFFECT_APPEND})
    handle = host.handle(A)
    sub = _Sub()
    handle.register_subscription(sub)
    host.purge(A)
    assert sub.closed is True
    assert host.handle(A) is None


def test_revoke_from_admitted_drains_first():
    host = PluginHost()
    host.discover(_manifest())
    host.validate(A)
    host.admit(A)
    handle = host.handle(A)
    sub = _Sub()
    handle.register_subscription(sub)
    host.revoke(A)
    assert sub.closed is True
    assert handle.grants.capabilities == frozenset()


# -- activation sequencing ----------------------------------------------------

def test_activate_never_bypasses_grant_requested_for_declared_capabilities():
    """DEFECT V6: VALIDATED -> ADMITTED direct skipped the grant step."""
    host = PluginHost()
    host.discover(_manifest(declared_capabilities=(Capability.WORKSPACE_READ,)))
    host.validate(A)
    with pytest.raises(PluginLifecycleError, match="never bound by bind_grants"):
        host.activate(A)


def test_activate_drives_declared_plugin_through_full_sequence():
    host = PluginHost()
    host.discover(_manifest(declared_capabilities=(Capability.WORKSPACE_READ,)))
    host.validate(A)
    host.bind_grants(A, {Capability.WORKSPACE_READ})
    ctx = host.activate(A)  # admit implied after binding
    assert host.state_of(A) is LifecycleState.ACTIVE
    assert ctx.facade.grants.has(Capability.WORKSPACE_READ)


def test_zero_grant_plugin_activates_as_default_untrusted():
    """Documented behaviour: zero-grant plugins are legitimate."""
    host = PluginHost()
    ctx = _bring_up(host)  # binds the empty set explicitly
    assert host.state_of(A) is LifecycleState.ACTIVE
    assert ctx.facade.grants.capabilities == frozenset()
    with pytest.raises(PluginCapabilityDenied):
        ctx.facade.query()


# -- monotonic terminality -----------------------------------------------------

def test_revoked_cannot_be_revived_by_activate_side_effect():
    host = PluginHost()
    _bring_up(host, capabilities={Capability.WORKSPACE_READ})
    host.revoke(A)
    with pytest.raises(PluginLifecycleError, match="monotonic"):
        host.activate(A)
    assert host.state_of(A) is LifecycleState.REVOKED


def test_revalidation_after_revoke_clears_grants_then_fresh_bind_works():
    host = PluginHost()
    _bring_up(host, capabilities={Capability.WORKSPACE_READ})
    host.revoke(A)
    host.validate(A)
    assert host.grants_of(A).capabilities == frozenset()
    ctx = _bring_up_again(host)
    assert ctx.facade.grants.capabilities == frozenset()


def _bring_up_again(host, plugin_id=A):
    host.bind_grants(plugin_id, set())
    host.admit(plugin_id)
    return host.activate(plugin_id)


def test_purged_handle_is_gone_and_unknown():
    host = PluginHost()
    host.discover(_manifest())
    host.validate(A)
    host.disable(A)
    host.purge(A)
    with pytest.raises(PluginLifecycleError, match="unknown plugin"):
        host.state_of(A)


# -- stale-surface invalidation (no GC-dependent correctness) ------------------

def test_stale_facade_refuses_reads_after_revoke(tmp_path):
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    host = PluginHost(roots={"ws": tmp_path})
    ctx = _bring_up(host, capabilities={Capability.WORKSPACE_READ})
    assert ctx.facade.query().read_text("ws", "a.txt") == "x\n"
    host.revoke(A)
    with pytest.raises(PluginCapabilityDenied, match="no longer live"):
        ctx.facade.query()


def test_stale_facade_refuses_effects_after_disable(tmp_path):
    host = PluginHost()
    ctx = _bring_up(host, capabilities={Capability.NAMED_EFFECT_APPEND})
    receipt = ctx.facade.append_named_effect("ok_before", {})
    assert receipt.outcome == "applied"
    host.disable(A)
    with pytest.raises(PluginCapabilityDenied, match="no longer live"):
        ctx.facade.append_named_effect("after_disable", {})


def test_stale_query_view_refuses_after_drain(tmp_path):
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    host = PluginHost(roots={"ws": tmp_path})
    ctx = _bring_up(host, capabilities={Capability.WORKSPACE_READ})
    view = ctx.facade.query()
    host.drain(A)
    with pytest.raises(PluginCapabilityDenied, match="no longer live"):
        view.read_text("ws", "a.txt")


def test_stale_extension_state_view_refuses_after_revoke():
    host = PluginHost()
    ctx = _bring_up(host, capabilities={Capability.EXTENSION_STATE})
    st = ctx.facade.extension_state()
    st.set("k", "v")
    host.revoke(A)
    with pytest.raises(PluginCapabilityDenied, match="no longer live"):
        st.get("k")
    with pytest.raises(PluginCapabilityDenied, match="no longer live"):
        st.keys()


def test_stale_grant_view_sees_nothing_after_revoke():
    """DEFECT V4/V9: the retained GrantSet view still advertised grants."""
    host = PluginHost()
    ctx = _bring_up(host, capabilities={Capability.WORKSPACE_READ})
    grants = ctx.grants
    assert grants.has(Capability.WORKSPACE_READ) is True
    host.revoke(A)
    assert grants.has(Capability.WORKSPACE_READ) is False
    assert grants.capabilities == frozenset()
    with pytest.raises(PluginCapabilityDenied, match="no longer live"):
        grants.require(Capability.WORKSPACE_READ)


def test_reactivation_invalidates_previous_generation_surfaces(tmp_path):
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    host = PluginHost(roots={"ws": tmp_path})
    ctx1 = _bring_up(host, capabilities={Capability.WORKSPACE_READ})
    v1 = ctx1.facade.query()
    host.drain(A)
    host.disable(A)
    host.validate(A)
    host.bind_grants(A, {Capability.WORKSPACE_READ})
    host.admit(A)
    ctx2 = host.activate(A)
    assert ctx2.facade.query().read_text("ws", "a.txt") == "x\n"
    with pytest.raises(PluginCapabilityDenied, match="no longer live"):
        v1.read_text("ws", "a.txt")


# -- re-validation re-parses the declaration -----------------------------------

def test_revalidation_rejects_a_drifted_cached_manifest():
    """DEFECT V7: validate() trusted the cached object after DISABLED."""
    host = PluginHost()
    host.discover(_manifest())
    host.validate(A)
    host.disable(A)
    handle = host.handle(A)
    # Simulate drift while parked: the cached manifest no longer satisfies
    # the host contract (schema bumped out from under the handle).
    object.__setattr__(handle.manifest, "manifest_schema_version", 99)
    with pytest.raises(Exception, match="unsupported manifest_schema_version"):
        host.validate(A)
    assert handle.state is LifecycleState.DISABLED, (
        "refused revalidation must not move the handle into VALIDATED"
    )


# -- grant minting surface --------------------------------------------------------

def test_host_mintable_copy_still_mints_for_authority_callers():
    from bots5.plugins import default_grants

    authority = default_grants(A)
    widened = authority.grant(Capability.WORKSPACE_READ)
    assert widened.has(Capability.WORKSPACE_READ) is True


def test_plugin_view_grant_set_cannot_mint_or_narrow():
    host = PluginHost()
    ctx = _bring_up(host, capabilities={Capability.WORKSPACE_READ})
    view = ctx.grants
    with pytest.raises(PluginCapabilityDenied, match="cannot mint grants"):
        view.grant(Capability.EVENT_SUBSCRIBE)
    with pytest.raises(PluginCapabilityDenied, match="cannot mutate grants"):
        view.revoke(Capability.WORKSPACE_READ)


# -- event subscription seam -------------------------------------------------------

class _FakeBus:
    def __init__(self):
        self.subscribe_calls = 0
        self.subs = []

    def subscribe(self):
        self.subscribe_calls += 1
        sub = _Sub()
        self.subs.append(sub)
        return sub


def test_event_subscription_requires_active_and_grant():
    host = PluginHost()
    host.discover(_manifest())
    host.validate(A)
    bus = _FakeBus()
    with pytest.raises(PluginLifecycleError, match="requires an ACTIVE plugin"):
        host.open_event_subscription(A, bus)
    host.bind_grants(A, {Capability.EVENT_SUBSCRIBE})
    host.admit(A)
    with pytest.raises(PluginLifecycleError, match="requires an ACTIVE plugin"):
        host.open_event_subscription(A, bus)
    assert bus.subscribe_calls == 0


def test_event_subscription_denied_without_grant_even_when_active():
    host = PluginHost()
    host.discover(_manifest())
    host.validate(A)
    host.bind_grants(A, {Capability.WORKSPACE_READ})
    host.admit(A)
    host.activate(A)
    bus = _FakeBus()
    with pytest.raises(PluginCapabilityDenied, match="event.subscribe"):
        host.open_event_subscription(A, bus)
    assert bus.subscribe_calls == 0


def test_event_subscription_closed_explicitly_on_drain():
    host = PluginHost()
    ctx = _bring_up(host, capabilities={Capability.EVENT_SUBSCRIBE})
    bus = _FakeBus()
    sub = host.open_event_subscription(A, bus)
    assert bus.subscribe_calls == 1
    host.drain(A)
    assert sub.closed is True
    assert host.handle(A).subscriptions() == ()


def test_event_subscription_uses_host_bound_bus_and_no_second_authority():
    host_bus = _FakeBus()
    host = PluginHost(events=host_bus)
    _bring_up(host, capabilities={Capability.EVENT_SUBSCRIBE})
    sub = host.open_event_subscription(A)
    assert host_bus.subscribe_calls == 1
    assert sub in host_bus.subs
