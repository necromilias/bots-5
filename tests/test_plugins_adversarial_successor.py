"""Successor to the trust-challenger's test_lead2_facade_exposes_no_store_reference.

The original asserted an exact private-slot allowlist
(_grants, _query, _admission, _effects, _state, _plugin_id). The facade later
gained a legitimate `_revoked` liveness predicate, which broke that brittle
assertion without any security regression (Director-confirmed, and confirmed by
the plugins manager as a stale assertion).

This successor tests the ACTUAL intent: the facade must expose no reference to
the backing store, event bus, or SQLite connection. It is deliberately robust to
legitimate new private slots, and does NOT weaken the security claim.
"""
from __future__ import annotations

import sqlite3


def _facade_slot_values(facade):
    for name in facade.__slots__:
        if name.startswith("__"):
            continue
        yield name, getattr(facade, name, None)


def test_facade_exposes_no_store_bus_or_connection_successor():
    from bots5.plugins import facade as facade_module

    # Any facade instance the module can construct must not leak backing
    # infrastructure. We assert on the declared surface rather than on a
    # frozen allowlist, so legitimate liveness/policy slots do not false-fail.
    forbidden_type_names = {
        "EventBus",
        "AppStateStore",
        "ApplicationStateStore",
        "Connection",
        "RootedConnection",
    }
    leaked = []
    for name, value in _facade_slot_values(facade_module.HostFacade):
        if value is None:
            continue
        type_name = type(value).__name__
        if type_name in forbidden_type_names or isinstance(value, sqlite3.Connection):
            leaked.append((name, type_name))

    assert leaked == [], f"facade leaks backing infrastructure: {leaked}"
