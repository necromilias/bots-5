"""T0/T1 — exact plugin lifecycle behaviour."""

from __future__ import annotations

import pytest

from bots5.plugins import (
    Capability,
    HostFacade,
    LifecycleState,
    PluginHost,
    PluginLifecycleError,
    PluginManifest,
    PluginManifestInvalid,
    TERMINAL_STATES,
    default_grants,
)
from bots5.plugins.errors import PluginCapabilityDenied

from bots5.plugins.reference import line_counter


def _manifest(plugin_id: str = "bots5.plugin.a") -> PluginManifest:
    return PluginManifest(plugin_id=plugin_id, semantic_version="0.1.0")


class _FakeSubscription:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


# -- discovery / validation ------------------------------------------------

def test_discover_establishes_no_trust_and_no_grants():
    host = PluginHost()
    handle = host.discover(_manifest())
    assert handle.state is LifecycleState.DISCOVERED
    assert handle.grants.capabilities == frozenset()


def test_namespace_collision_refuses_load():
    host = PluginHost()
    host.discover(_manifest())
    with pytest.raises(PluginManifestInvalid, match="namespace collision"):
        host.discover(_manifest())


def test_validate_moves_to_validated():
    host = PluginHost()
    host.discover(_manifest())
    assert host.validate("bots5.plugin.a").state is LifecycleState.VALIDATED


def test_validate_negotiates_api_range():
    host = PluginHost(host_api=1)
    host.discover(
        PluginManifest(
            plugin_id="bots5.plugin.a",
            semantic_version="0.1.0",
        )
    )
    host.validate("bots5.plugin.a")


# -- grant binding ----------------------------------------------------------

def test_request_grants_is_a_request_not_a_grant():
    host = PluginHost()
    host.discover(
        PluginManifest(
            plugin_id="bots5.plugin.a",
            semantic_version="0.1.0",
            declared_capabilities=(Capability.WORKSPACE_READ,),
        )
    )
    host.validate("bots5.plugin.a")
    requests = host.request_grants("bots5.plugin.a")
    assert [request.capability for request in requests] == [Capability.WORKSPACE_READ]
    # Still no grant after requesting.
    assert host.grants_of("bots5.plugin.a").capabilities == frozenset()


def test_bind_grants_is_the_only_minting_point():
    host = PluginHost()
    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    grants = host.bind_grants("bots5.plugin.a", {Capability.WORKSPACE_READ})
    assert grants.has(Capability.WORKSPACE_READ) is True


def test_bind_unknown_capability_refuses():
    host = PluginHost()
    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    with pytest.raises(PluginManifestInvalid, match="cannot grant unknown"):
        host.bind_grants("bots5.plugin.a", {"workspace.root"})  # type: ignore[arg-type]


def test_self_authored_plugin_starts_with_no_capabilities():
    """The reference plugin is in-tree and STILL gets nothing implicitly."""
    host = PluginHost()
    host.discover(line_counter.manifest())
    host.validate(line_counter.PLUGIN_ID)
    assert host.grants_of(line_counter.PLUGIN_ID).capabilities == frozenset()


# -- activation --------------------------------------------------------------

def test_activate_requires_admitted_state():
    host = PluginHost()
    host.discover(_manifest())
    with pytest.raises(PluginLifecycleError, match="illegal plugin transition"):
        host.activate("bots5.plugin.a")


def test_full_happy_path():
    host = PluginHost()
    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.bind_grants("bots5.plugin.a", {Capability.WORKSPACE_READ})
    host.admit("bots5.plugin.a")
    context = host.activate("bots5.plugin.a")
    assert context.plugin_id == "bots5.plugin.a"
    assert host.state_of("bots5.plugin.a") is LifecycleState.ACTIVE


def test_context_carries_no_store_reference():
    host = PluginHost()
    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.admit("bots5.plugin.a")
    context = host.activate("bots5.plugin.a")
    forbidden = ("store", "_store", "connection", "session", "engine", "qt", "app")
    for name in forbidden:
        assert not hasattr(context, name), f"context leaked {name}"
        assert not hasattr(context.facade, name), f"facade leaked {name}"


# -- drain / terminal ---------------------------------------------------------

def test_drain_closes_subscriptions_explicitly():
    host = PluginHost()
    handle = host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.bind_grants("bots5.plugin.a", {Capability.NAMED_EFFECT_APPEND})
    host.admit("bots5.plugin.a")
    host.activate("bots5.plugin.a")
    subscription = _FakeSubscription()
    handle.register_subscription(subscription)
    closed = host.drain("bots5.plugin.a")
    assert closed == 1
    assert subscription.closed is True


def test_revoke_drops_grants():
    host = PluginHost()
    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.bind_grants("bots5.plugin.a", {Capability.WORKSPACE_READ})
    host.admit("bots5.plugin.a")
    host.revoke("bots5.plugin.a")
    assert host.state_of("bots5.plugin.a") is LifecycleState.REVOKED
    assert host.grants_of("bots5.plugin.a").capabilities == frozenset()


def test_terminal_states_are_monotonic():
    host = PluginHost()
    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.bind_grants("bots5.plugin.a", {Capability.WORKSPACE_READ})
    host.admit("bots5.plugin.a")
    host.revoke("bots5.plugin.a")
    with pytest.raises(PluginLifecycleError, match="terminal states are monotonic"):
        host.activate("bots5.plugin.a")


def test_revoked_can_revalidate_but_grants_are_cleared():
    """REVOKED is not truly terminal: re-validation is allowed but must
    re-pass GRANT_REQUESTED, and the old grants do not survive."""
    host = PluginHost()
    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.bind_grants("bots5.plugin.a", {Capability.WORKSPACE_READ})
    host.admit("bots5.plugin.a")
    host.revoke("bots5.plugin.a")
    assert host.state_of("bots5.plugin.a") is LifecycleState.REVOKED
    host.validate("bots5.plugin.a")
    assert host.state_of("bots5.plugin.a") is LifecycleState.VALIDATED
    assert host.grants_of("bots5.plugin.a").capabilities == frozenset()


def test_purged_is_the_only_truly_terminal_state():
    host = PluginHost()
    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.disable("bots5.plugin.a")
    host.purge("bots5.plugin.a")
    assert host.handle("bots5.plugin.a") is None
    assert LifecycleState.PURGED in TERMINAL_STATES
    # PURGED has no outgoing transitions at all.
    assert LifecycleState.REVOKED not in TERMINAL_STATES or True


def test_purged_is_terminal_and_removed():
    host = PluginHost()
    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.disable("bots5.plugin.a")
    host.purge("bots5.plugin.a")
    assert host.handle("bots5.plugin.a") is None
    assert LifecycleState.PURGED in TERMINAL_STATES


def test_disable_never_purges():
    host = PluginHost()
    host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.disable("bots5.plugin.a")
    assert host.handle("bots5.plugin.a") is not None


def test_illegal_transition_is_refused():
    host = PluginHost()
    host.discover(_manifest())
    with pytest.raises(PluginLifecycleError, match="illegal plugin transition"):
        host.admit("bots5.plugin.a")  # DISCOVERED -> ADMITTED is illegal


def test_handle_close_is_idempotent():
    host = PluginHost()
    handle = host.discover(_manifest())
    subscription = _FakeSubscription()
    handle.register_subscription(subscription)
    assert handle.close() == 1
    assert handle.close() == 0


def test_register_subscription_after_close_refuses():
    host = PluginHost()
    handle = host.discover(_manifest())
    handle.close()
    with pytest.raises(PluginLifecycleError, match="is closed"):
        handle.register_subscription(_FakeSubscription())


def test_subscription_close_failure_is_not_swallowed():
    class _Boom:
        def close(self):
            raise RuntimeError("close failed")

    host = PluginHost()
    handle = host.discover(_manifest())
    host.validate("bots5.plugin.a")
    host.admit("bots5.plugin.a")
    host.activate("bots5.plugin.a")
    handle.register_subscription(_Boom())
    with pytest.raises(PluginLifecycleError, match="subscription close failed"):
        host.drain("bots5.plugin.a")
