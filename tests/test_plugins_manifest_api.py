"""T0/T1 — plugin manifest/API behaviour.

Exact contract under test: a declared capability is a REQUEST, never a
grant; manifests fail closed; self-authorship grants nothing.
"""

from __future__ import annotations

import pytest

from bots5.plugins import (
    HOST_API_VERSION,
    HOST_CAPABILITIES,
    MANIFEST_SCHEMA_VERSION,
    Capability,
    CapabilityRequest,
    GrantSet,
    PluginCapabilityDenied,
    PluginIncompatible,
    PluginManifest,
    PluginManifestInvalid,
    HostApiRange,
    manifest_from_mapping,
)


# -- capability request is not a grant ------------------------------------

def test_request_carries_no_authority():
    request = CapabilityRequest(Capability.WORKSPACE_READ)
    grants = GrantSet("bots5.plugin.x")
    # Holding a request object must not create a grant.
    assert grants.has(request.capability) is False
    with pytest.raises(PluginCapabilityDenied, match="workspace.read"):
        grants.require(request.capability)


def test_default_grant_set_is_empty_even_for_self_authored():
    grants = GrantSet("bots5.plugin.reference.line_count")
    assert grants.capabilities == frozenset()
    for capability in Capability:
        assert grants.has(capability) is False
        with pytest.raises(PluginCapabilityDenied):
            grants.require(capability)


def test_grant_is_explicit_and_returns_new_set():
    grants = GrantSet("bots5.plugin.x")
    extended = grants.grant(Capability.WORKSPACE_READ)
    assert grants.has(Capability.WORKSPACE_READ) is False, "grant must not mutate in place"
    assert extended.has(Capability.WORKSPACE_READ) is True


def test_revoke_removes_capability():
    grants = GrantSet("bots5.plugin.x", frozenset({Capability.WORKSPACE_READ}))
    reduced = grants.revoke(Capability.WORKSPACE_READ)
    assert reduced.has(Capability.WORKSPACE_READ) is False


def test_unknown_capability_string_is_refused():
    with pytest.raises(PluginCapabilityDenied, match="not a member"):
        CapabilityRequest("workspace.superuser")  # type: ignore[arg-type]


def test_capability_enum_is_closed():
    assert Capability.WORKSPACE_READ in HOST_CAPABILITIES
    with pytest.raises(ValueError):
        Capability("workspace.delete_everything")


# -- manifest validation fails closed -------------------------------------

def test_manifest_requires_reserved_namespace():
    with pytest.raises(PluginManifestInvalid, match="reserved namespace"):
        PluginManifest(plugin_id="evil.plugin.thing", semantic_version="0.1.0")


def test_manifest_rejects_empty_leaf():
    with pytest.raises(PluginManifestInvalid, match="empty leaf"):
        PluginManifest(plugin_id="bots5.plugin.", semantic_version="0.1.0")


def test_minimal_manifest_declares_no_capabilities_by_default():
    manifest = PluginManifest(plugin_id="bots5.plugin.a", semantic_version="0.1.0")
    assert manifest.declared_capabilities == ()
    assert manifest.requested_capability_names == ()


def test_unknown_field_fails_closed():
    with pytest.raises(PluginManifestInvalid, match="unknown manifest fields fail closed"):
        manifest_from_mapping(
            {
                "plugin_id": "bots5.plugin.a",
                "semantic_version": "0.1.0",
                "trust_level": "trusted",
            }
        )


def test_trust_level_is_not_a_manifest_field():
    """Trust derives from ratified policy, never from self-declaration."""
    manifest = manifest_from_mapping(
        {"plugin_id": "bots5.plugin.a", "semantic_version": "0.1.0"}
    )
    assert not hasattr(manifest, "trust_level")


def test_unknown_capability_in_manifest_refuses_load():
    with pytest.raises(PluginManifestInvalid, match="host capability enum is closed"):
        manifest_from_mapping(
            {
                "plugin_id": "bots5.plugin.a",
                "semantic_version": "0.1.0",
                "declared_capabilities": ["workspace.read_everything"],
            }
        )


def test_missing_required_fields_refuse():
    with pytest.raises(PluginManifestInvalid, match="missing required"):
        manifest_from_mapping({"plugin_id": "bots5.plugin.a"})


def test_manifest_schema_version_mismatch_refuses():
    with pytest.raises(PluginManifestInvalid, match="unsupported manifest_schema_version"):
        manifest_from_mapping(
            {
                "plugin_id": "bots5.plugin.a",
                "semantic_version": "0.1.0",
                "manifest_schema_version": MANIFEST_SCHEMA_VERSION + 1,
            }
        )


def test_api_range_unsatisfiable_is_incompatible_not_loaded():
    manifest = PluginManifest(
        plugin_id="bots5.plugin.a",
        semantic_version="0.1.0",
        api_range=HostApiRange(min_host_api=HOST_API_VERSION + 1, max_host_api=HOST_API_VERSION + 1),
    )
    with pytest.raises(PluginIncompatible, match="requires host api"):
        manifest.negotiate(HOST_API_VERSION)


def test_api_range_is_a_range_not_equality():
    manifest = PluginManifest(
        plugin_id="bots5.plugin.a",
        semantic_version="0.1.0",
        api_range=HostApiRange(min_host_api=1, max_host_api=HOST_API_VERSION),
    )
    manifest.negotiate(HOST_API_VERSION)
    assert manifest.api_range.satisfied_by(1) is True


def test_inverted_api_range_is_invalid():
    with pytest.raises(PluginManifestInvalid):
        HostApiRange(min_host_api=5, max_host_api=1)


def test_manifest_parses_capability_dict_entries():
    manifest = manifest_from_mapping(
        {
            "plugin_id": "bots5.plugin.a",
            "semantic_version": "0.1.0",
            "declared_capabilities": [
                {"capability": "workspace.read", "justification": "count lines"}
            ],
        }
    )
    assert manifest.requested_capability_names == ("workspace.read",)


def test_unknown_capability_field_fails_closed():
    with pytest.raises(PluginManifestInvalid, match="unknown capability fields"):
        manifest_from_mapping(
            {
                "plugin_id": "bots5.plugin.a",
                "semantic_version": "0.1.0",
                "declared_capabilities": [{"capability": "workspace.read", "scope": "all"}],
            }
        )
