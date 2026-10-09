"""T0/T1 — WORKER-02 (plugin-lifecycle) facade-narrowness & reference-plugin suite.

Distinctly named so it never clobbers the api-manifest sibling's files.
Verifies, with executable evidence rather than prose:

* the host facade is NARROW and is not the ~70-method AppStateStore;
* no seam reachable from a PluginContext exposes a store, Qt object, SQLite
  connection or the effect-admission callable itself;
* the ONE self-authored reference plugin starts with ZERO capabilities and
  works only via explicit grants — including after reactivation cycles;
* receipt truthfulness: a pre-issue refusal is recorded as ``refused``, not
  dressed up as terminal ``UNKNOWN`` (the campaign's UNKNOWN means an issued
  effect whose outcome is unestablished);
* workspace containment holds against symlink escape attempts.
"""

from __future__ import annotations

import inspect
from contextlib import nullcontext
from pathlib import Path

import pytest

from bots5.plugins import (
    Capability,
    GrantSet,
    HostFacade,
    LifecycleState,
    PluginCapabilityDenied,
    PluginHost,
    PluginManifest,
    ReadOnlyQueryView,
    manifest_from_mapping,
)
from bots5.plugins.facade import EFFECT_OUTCOMES, EffectReceipt
from bots5.plugins.reference import line_counter

A = "bots5.plugin.a"


class _Sub:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _bring_up(host, plugin_id=A, capabilities=frozenset(), roots=None):
    host.discover(PluginManifest(plugin_id=plugin_id, semantic_version="0.1.0"))
    host.validate(plugin_id)
    host.bind_grants(plugin_id, set(capabilities))
    host.admit(plugin_id)
    return host.activate(plugin_id)


# -- facade width vs the wide store ------------------------------------------

def test_facade_surface_is_a_small_fraction_of_app_state_store():
    """The facade must NOT be the ~70-method AppStateStore Protocol."""
    from bots5.core.ports import AppStateStore

    store_methods = {
        name
        for name, member in inspect.getmembers(AppStateStore, predicate=callable)
        if not name.startswith("_")
    }
    facade_members = {
        name
        for name in dir(HostFacade)
        if not name.startswith("_") and not isinstance(getattr(HostFacade, name), property)
    }
    facade_all = {name for name in dir(HostFacade) if not name.startswith("_")}
    assert len(store_methods) >= 60, "AppStateStore is the wide ~70-method surface"
    # Public plugin-facing surface: at most the documented seams.
    assert facade_all <= {
        "plugin_id",
        "grants",
        "query",
        "append_named_effect",
        "effects",
        "extension_state",
    }, f"facade public surface widened: {facade_all}"
    assert len(facade_all) * 4 < len(store_methods), "facade is not narrow"


def test_no_context_attribute_reveals_the_wide_store_or_admission():
    host = PluginHost()
    ctx = _bring_up(
        host,
        capabilities={Capability.WORKSPACE_READ, Capability.NAMED_EFFECT_APPEND},
    )
    # The admission callable is private and never exposed as a usable seam.
    assert not hasattr(ctx.facade, "admission")
    assert not hasattr(ctx, "store")
    # Walking one level out from every public object finds no engine/conn/qt.
    seen = [ctx, ctx.facade, ctx.grants]
    for obj in seen:
        for name in dir(obj):
            if name.startswith("__"):
                continue
            try:
                value = getattr(obj, name)
            except Exception:
                continue
            kind = type(value).__name__
            low = kind.lower()
            assert "sqlite" not in low and "engine" not in low, f"{obj}.{name}: {kind}"
            assert not low.startswith("q"), f"{obj}.{name}: Qt object {kind}"
            assert "session" not in low, f"{obj}.{name}: {kind}"


def test_extension_state_view_has_no_sql_or_transaction_surface():
    host = PluginHost()
    ctx = _bring_up(host, capabilities={Capability.EXTENSION_STATE})
    st = ctx.facade.extension_state()
    public = {n for n in dir(st) if not n.startswith("_")}
    assert public <= {"get", "set", "delete", "keys"}
    for forbidden in ("execute", "commit", "rollback", "sql", "connection"):
        assert not hasattr(st, forbidden)


# -- receipt truthfulness ------------------------------------------------------

def test_pre_issue_admission_refusal_is_refused_not_unknown():
    """DEFECT V8: a failed admission was labelled UNKNOWN although the
    effect provably never ran.  UNKNOWN must stay reserved for genuinely
    unestablished outcomes of *issued* effects."""

    def boom_admission():
        return nullcontext()  # placeholder replaced below

    from contextlib import contextmanager

    @contextmanager
    def refusing():
        raise RuntimeError("admission refused")
        yield

    grants = GrantSet(A, frozenset({Capability.NAMED_EFFECT_APPEND}))
    facade = HostFacade(grants, admission=refusing)
    receipt = facade.append_named_effect("x", {})
    assert receipt.outcome == "refused"
    assert "effect not applied" in receipt.detail
    assert receipt.effect_name == "x"


def test_receipt_outcome_vocabulary_is_closed():
    assert "UNKNOWN" in EFFECT_OUTCOMES and "refused" in EFFECT_OUTCOMES
    with pytest.raises(PluginCapabilityDenied, match="receipt vocabulary is closed"):
        EffectReceipt("x", "probably_applied")


def test_successful_effect_still_receipts_applied():
    grants = GrantSet(A, frozenset({Capability.NAMED_EFFECT_APPEND}))
    facade = HostFacade(grants)  # no admission supplied by this host
    receipt = facade.append_named_effect("ok", {})
    assert receipt.outcome == "applied"
    assert facade.effects()[-1] is receipt


# -- workspace containment: symlink escape --------------------------------------

def test_symlink_inside_root_cannot_read_outside(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET\n", encoding="utf-8")
    root = tmp_path / "ws"
    root.mkdir()
    (root / "link.txt").symlink_to(secret)
    grants = GrantSet(A, frozenset({Capability.WORKSPACE_READ}))
    view = ReadOnlyQueryView(grants, {"ws": root})
    with pytest.raises(PluginCapabilityDenied, match="escapes the granted root"):
        view.read_text("ws", "link.txt")


def test_directory_symlink_cannot_leak_listing(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "z.txt").write_text("z\n", encoding="utf-8")
    root = tmp_path / "ws"
    root.mkdir()
    (root / "dlink").symlink_to(outside, target_is_directory=True)
    grants = GrantSet(A, frozenset({Capability.WORKSPACE_READ}))
    view = ReadOnlyQueryView(grants, {"ws": root})
    with pytest.raises(PluginCapabilityDenied, match="escapes the granted root"):
        view.list_names("ws", "dlink")


# -- reference plugin: zero ambient capability, works only via grants ------------

def test_reference_plugin_manifest_declares_only_reads():
    m = line_counter.manifest()
    assert m.requested_capability_names == ("workspace.read",)
    # Declared != granted: constructing the manifest minted nothing.
    host = PluginHost()
    host.discover(m)
    host.validate(line_counter.PLUGIN_ID)
    assert host.grants_of(line_counter.PLUGIN_ID).capabilities == frozenset()


def test_reference_plugin_denied_with_zero_grants_end_to_end(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    host = PluginHost(roots={"ws": tmp_path})
    ctx = _bring_up(host, plugin_id=line_counter.PLUGIN_ID)
    with pytest.raises(PluginCapabilityDenied, match="workspace.read"):
        line_counter.count_lines(ctx, "ws", "a.txt")


def test_reference_plugin_works_after_explicit_grant(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    host = PluginHost(roots={"ws": tmp_path})
    host.discover(line_counter.manifest())
    host.validate(line_counter.PLUGIN_ID)
    host.bind_grants(line_counter.PLUGIN_ID, {Capability.WORKSPACE_READ})
    host.admit(line_counter.PLUGIN_ID)
    ctx = host.activate(line_counter.PLUGIN_ID)
    result = line_counter.count_lines(ctx, "ws", "a.txt")
    assert result.lines == 3
    results = line_counter.count_lines_all(ctx, "ws")
    assert results == (line_counter.LineCountResult("a.txt", 3),)


def test_reference_plugin_grant_dies_with_activation(tmp_path):
    """Self-authored plugins get nothing implicitly — and explicitly granted
    authority dies when the host drains/revokes the activation."""
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    host = PluginHost(roots={"ws": tmp_path})
    host.discover(line_counter.manifest())
    host.validate(line_counter.PLUGIN_ID)
    host.bind_grants(line_counter.PLUGIN_ID, {Capability.WORKSPACE_READ})
    host.admit(line_counter.PLUGIN_ID)
    ctx = host.activate(line_counter.PLUGIN_ID)
    assert line_counter.count_lines(ctx, "ws", "a.txt").lines == 1
    host.revoke(line_counter.PLUGIN_ID)
    with pytest.raises(PluginCapabilityDenied, match="no longer live"):
        line_counter.count_lines(ctx, "ws", "a.txt")


def test_reference_plugin_survives_full_lifecycle_cycle(tmp_path):
    (tmp_path / "a.txt").write_text("x\ny\n", encoding="utf-8")
    host = PluginHost(roots={"ws": tmp_path})
    ctx = _bring_up(host, plugin_id=line_counter.PLUGIN_ID,
                    capabilities={Capability.WORKSPACE_READ})
    assert line_counter.count_lines(ctx, "ws", "a.txt").lines == 2
    handle = host.handle(line_counter.PLUGIN_ID)
    sub = _Sub()
    handle.register_subscription(sub)
    host.drain(line_counter.PLUGIN_ID)
    assert sub.closed is True
    host.disable(line_counter.PLUGIN_ID)
    host.validate(line_counter.PLUGIN_ID)
    assert host.grants_of(line_counter.PLUGIN_ID).capabilities == frozenset()
    host.bind_grants(line_counter.PLUGIN_ID, {Capability.WORKSPACE_READ})
    host.admit(line_counter.PLUGIN_ID)
    ctx2 = host.activate(line_counter.PLUGIN_ID)
    assert line_counter.count_lines(ctx2, "ws", "a.txt").lines == 2


# -- manifest round-trip used by revalidation ------------------------------------

def test_as_raw_declaration_round_trips_through_the_fail_closed_parser():
    m = line_counter.manifest()
    rebuilt = PluginManifest(**m.as_raw_declaration())
    assert rebuilt == m
    raw = manifest_from_mapping(
        {
            "plugin_id": A,
            "semantic_version": "0.1.0",
            "declared_capabilities": ["workspace.read"],
        }
    )
    assert PluginManifest(**raw.as_raw_declaration()) == raw
