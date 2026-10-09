"""Expected plugin-framework failures.

Every error here is a *typed* failure.  Denials are never swallowed and
never degrade into a permissive default: a plugin that lacks a capability
receives ``PluginCapabilityDenied`` naming the missing capability.
"""

from __future__ import annotations

from bots5.errors import Bots5Error


class PluginError(Bots5Error):
    """Base class for expected plugin-framework failures."""


class PluginManifestInvalid(PluginError):
    """A manifest violates the schema; the plugin must not be loaded."""


class PluginIncompatible(PluginError):
    """A manifest is well-formed but the host cannot satisfy its api_range."""


class PluginLifecycleError(PluginError):
    """A requested lifecycle transition is not legal from the current state."""


class PluginCapabilityDenied(PluginError):
    """A plugin attempted an effect it was not granted.

    This is the fail-closed refusal path.  It always names the capability
    that was missing so the denial is auditable rather than silent.
    """

    def __init__(self, capability: str, *, plugin_id: str = "", detail: str = "") -> None:
        self.capability = capability
        self.plugin_id = plugin_id
        parts = [f"plugin capability denied: {capability}"]
        if plugin_id:
            parts.append(f"plugin_id={plugin_id}")
        if detail:
            parts.append(detail)
        super().__init__("; ".join(parts))
