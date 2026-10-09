"""Self-authored plugin framework — capability-bound consumer, no ambient trust.

This package implements the plugin manifest/API, lifecycle and capability
request/grant binding for *self-authored* plugins.  Self-authored status
grants **no** ambient trust: a plugin starts with zero capabilities and
operates only through explicit grants issued by the authority layer.

Design commitments frozen by the parent campaign (see
``docs/PLUGINS_V0_2.md``) and inherited from the sealed research:

* A declared capability is a **request**, never a grant.
* There is exactly one authority per data root; a plugin never becomes a
  second authority.  Plugin effects take or join the existing
  ``command_admission`` / ``event_admission`` / ``issued_event_effect``
  effect grant at the callee boundary.  This package deliberately does NOT
  invent a second authority system.
* No direct Qt access, no global mutable access and no core SQLite
  connection reach plugin code.
* Correctness never depends on garbage collection or destruction timing:
  event subscriptions are closed explicitly and drain precedes terminal
  states.
* Unknown manifest fields fail closed; capability names come from a closed
  enum known to the host (open enums would let a compromised extension
  define its own authority).
"""

from __future__ import annotations

from .capabilities import (
    HOST_CAPABILITIES,
    Capability,
    CapabilityDenied,
    CapabilityGrant,
    CapabilityRequest,
    GrantSet,
    default_grants,
    validate_requests,
)
from .errors import (
    PluginCapabilityDenied,
    PluginError,
    PluginIncompatible,
    PluginLifecycleError,
    PluginManifestInvalid,
)
from .facade import HostFacade, PluginContext, ReadOnlyQueryView
from .lifecycle import (
    LifecycleState,
    PluginHandle,
    PluginHost,
    TERMINAL_STATES,
)
from .manifest import (
    MANIFEST_SCHEMA_VERSION,
    AbsentSurfacePolicy,
    EntrypointKind,
    HostApiRange,
    PluginManifest,
    PluginSurface,
    HOST_API_VERSION,
    manifest_from_mapping,
)

__all__ = [
    "HOST_API_VERSION",
    "HOST_CAPABILITIES",
    "MANIFEST_SCHEMA_VERSION",
    "TERMINAL_STATES",
    "AbsentSurfacePolicy",
    "Capability",
    "CapabilityDenied",
    "CapabilityGrant",
    "CapabilityRequest",
    "EntrypointKind",
    "GrantSet",
    "HostApiRange",
    "HostFacade",
    "LifecycleState",
    "PluginCapabilityDenied",
    "PluginContext",
    "PluginError",
    "PluginHandle",
    "PluginHost",
    "PluginIncompatible",
    "PluginLifecycleError",
    "PluginManifest",
    "PluginManifestInvalid",
    "PluginSurface",
    "ReadOnlyQueryView",
    "default_grants",
    "manifest_from_mapping",
    "validate_requests",
]
