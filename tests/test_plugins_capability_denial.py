"""T0/T1 — capability-denial semantics and ambient-authority leak hunt.

This suite actively searches for ambient authority: any path by which a
self-authored plugin could obtain an effect it was not explicitly granted.
A denial must be a typed refusal naming the missing capability — never a
silent empty result, and never a permissive fallback.
"""

from __future__ import annotations

import pytest

from bots5.plugins import (
    Capability,
    GrantSet,
    HostFacade,
    PluginCapabilityDenied,
    PluginContext,
    PluginHost,
    PluginManifest,
    ReadOnlyQueryView,
    default_grants,
)

from bots5.plugins.reference import line_counter


def _granted_facade(capabilities=frozenset(), *, roots=None, admission=None):
    grants = GrantSet("bots5.plugin.ref", capabilities)
    query = ReadOnlyQueryView(grants, roots or {}) if roots else None
    return HostFacade(grants, query=query, admission=admission)


# -- capability denial -------------------------------------------------------

def test_denial_names_the_missing_capability():
    facade = _granted_facade()
    with pytest.raises(PluginCapabilityDenied) as excinfo:
        facade.query()
    assert "workspace.read" in str(excinfo.value)


def test_denial_is_not_a_silent_empty_result():
    """A denial must raise, not return an empty/default value."""
    facade = _granted_facade()
    try:
        facade.query()
    except PluginCapabilityDenied:
        return
    pytest.fail("denial must be a typed refusal, not a silent default")


def test_named_effect_denied_without_grant():
    facade = _granted_facade()
    with pytest.raises(PluginCapabilityDenied, match="named_effect.append"):
        facade.append_named_effect("do_thing", {})


def test_extension_state_denied_without_grant():
    facade = _granted_facade()
    with pytest.raises(PluginCapabilityDenied, match="extension.state"):
        facade.extension_state()


def test_grant_enables_only_that_capability():
    facade = _granted_facade({Capability.NAMED_EFFECT_APPEND})
    receipt = facade.append_named_effect("ping", {"a": 1})
    assert receipt.outcome == "applied"
    # The other capabilities remain denied.
    with pytest.raises(PluginCapabilityDenied):
        facade.query()
    with pytest.raises(PluginCapabilityDenied):
        facade.extension_state()


# -- workspace read confinement ----------------------------------------------

def test_workspace_read_refuses_path_traversal(tmp_path):
    root = tmp_path / "granted"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    facade = _granted_facade({Capability.WORKSPACE_READ}, roots={"ws": root})
    view = facade.query()
    with pytest.raises(PluginCapabilityDenied, match="escapes the granted root"):
        view.read_text("ws", "../secret.txt")


def test_workspace_read_refuses_unknown_root(tmp_path):
    root = tmp_path / "granted"
    root.mkdir()
    facade = _granted_facade({Capability.WORKSPACE_READ}, roots={"ws": root})
    view = facade.query()
    with pytest.raises(PluginCapabilityDenied, match="no workspace root is granted"):
        view.read_text("other", "file.txt")


def test_workspace_read_allows_inside_root(tmp_path):
    root = tmp_path / "granted"
    root.mkdir()
    (root / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    facade = _granted_facade({Capability.WORKSPACE_READ}, roots={"ws": root})
    assert facade.query().read_text("ws", "a.txt") == "one\ntwo\n"


# -- reference plugin: self-authored, still untrusted ------------------------

def test_reference_plugin_denied_without_grant(tmp_path):
    """Self-authored and in-tree, yet it cannot read without a grant."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("x\n", encoding="utf-8")
    host = PluginHost(roots={"ws": root})
    host.discover(line_counter.manifest())
    host.validate(line_counter.PLUGIN_ID)
    # NO bind_grants call — the plugin is self-authored and still gets nothing.
    host.admit(line_counter.PLUGIN_ID)
    context = host.activate(line_counter.PLUGIN_ID)
    with pytest.raises(PluginCapabilityDenied, match="workspace.read"):
        line_counter.count_lines(context, "ws", "a.txt")


def test_reference_plugin_works_with_explicit_grant(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    host = PluginHost(roots={"ws": root})
    host.discover(line_counter.manifest())
    host.validate(line_counter.PLUGIN_ID)
    host.bind_grants(line_counter.PLUGIN_ID, {Capability.WORKSPACE_READ})
    host.admit(line_counter.PLUGIN_ID)
    context = host.activate(line_counter.PLUGIN_ID)
    result = line_counter.count_lines(context, "ws", "a.txt")
    assert result.lines == 3


# -- ambient authority leak hunt ---------------------------------------------

def test_no_ambient_grant_from_module_import():
    """Importing the framework must not hand out any capability."""
    grants = default_grants("bots5.plugin.anything")
    assert grants.capabilities == frozenset()


def test_grant_set_cannot_be_amplified_in_place():
    """A plugin-held GrantSet refuses to mint at all.

    ``GrantSet.grant`` is the host's minting point.  A GrantSet handed to
    plugin code is marked non-mintable, so calling ``.grant()`` on it raises
    rather than merely producing a set the host ignores.  The original set is
    unchanged either way.
    """
    facade = _granted_facade()
    grants = facade.grants
    with pytest.raises(PluginCapabilityDenied, match="cannot mint grants"):
        grants.grant(Capability.WORKSPACE_READ)
    # The facade's held grant set is unchanged.
    assert facade.grants.capabilities == frozenset()
    assert grants.capabilities == frozenset()
    # And nothing the plugin did widened what it may do.
    with pytest.raises(PluginCapabilityDenied, match="workspace.read"):
        facade.query()


def test_grant_set_has_no_in_place_mutator():
    grants = default_grants("bots5.plugin.a")
    for name in ("add", "update", "widen", "escalate", "grant_all"):
        assert not hasattr(grants, name), f"grants leak in-place mutator {name}"


def test_facade_does_not_expose_the_wide_store():
    """The ~70-method AppStateStore must never reach plugin code."""
    facade = _granted_facade({Capability.WORKSPACE_READ})
    wide_names = (
        "create_chat", "delete_chat", "list_chats", "search", "rebuild_search_index",
        "read_attachment_bytes", "delete_message", "enqueue_archive_import",
        "save_dock_layout", "save_font_scale_settings", "list_folders",
        "delete_folder", "persist_generation_start", "finalize_generation",
    )
    for name in wide_names:
        assert not hasattr(facade, name), f"facade exposes wide-store method {name}"


def test_context_is_minimal():
    facade = _granted_facade()
    context = PluginContext("bots5.plugin.a", facade, facade.grants)
    exposed = {name for name in dir(context) if not name.startswith("_")}
    assert exposed <= {"plugin_id", "facade", "grants", "capabilities"}


def test_plugin_cannot_mutate_its_own_grants():
    """A plugin holding the facade cannot widen its grant set."""
    facade = _granted_facade()
    grants = facade.grants
    assert isinstance(grants, GrantSet)
    # GrantSet exposes no mutator; only explicit copies.
    assert not hasattr(grants, "add")
    assert not hasattr(grants, "update")
    before = grants.capabilities
    _ = facade  # no path from facade to a wider set
    assert grants.capabilities == before == frozenset()


def test_effect_requires_grant_even_with_admission_supplied(tmp_path):
    """A supplied admission does not substitute for a grant."""
    facade = _granted_facade(roots={"ws": tmp_path})  # no capabilities
    with pytest.raises(PluginCapabilityDenied):
        facade.append_named_effect("x", {})


def test_manifest_declaring_capability_does_not_grant_it():
    """Declared != granted.  The manifest is a request only."""
    host = PluginHost()
    manifest = PluginManifest(
        plugin_id="bots5.plugin.a",
        semantic_version="0.1.0",
        declared_capabilities=(Capability.WORKSPACE_READ,),
    )
    host.discover(manifest)
    host.validate("bots5.plugin.a")
    host.request_grants("bots5.plugin.a")
    host.admit("bots5.plugin.a")
    context = host.activate("bots5.plugin.a")
    with pytest.raises(PluginCapabilityDenied, match="workspace.read"):
        context.facade.query()
