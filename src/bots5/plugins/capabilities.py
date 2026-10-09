"""Capability names, requests and grants.

The single most important invariant in this module:

    A capability is a REQUEST, never a grant.  Granting lives in the
    authority layer.  A manifest cannot widen a grant, and holding a
    ``CapabilityRequest`` confers no authority whatsoever.

The capability name set is a **closed enum known to the host**.  Open enums
are unsafe: they would let a compromised extension define its own authority
vocabulary and therefore its own blast radius.  An unknown capability name
fails closed at validation time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .errors import PluginCapabilityDenied


class Capability(str, Enum):
    """Closed set of effect classes a plugin may *request*.

    Self-authored plugins receive no ambient trust, so the default grant set
    for any plugin is empty regardless of who authored it.
    """

    #: Read-only inspection of an explicitly granted workspace path.
    WORKSPACE_READ = "workspace.read"
    #: Append-only named effect (bounded, receipted mutation).
    NAMED_EFFECT_APPEND = "named_effect.append"
    #: Subscribe to host events (bounded queue, explicit close).
    EVENT_SUBSCRIBE = "event.subscribe"
    #: Read/write extension-scoped key-value state.
    EXTENSION_STATE = "extension.state"


#: The host's authoritative capability vocabulary.  A manifest naming
#: anything outside this set is refused at validation.
HOST_CAPABILITIES: frozenset[Capability] = frozenset(Capability)


# Mapping from plugin Capability to CapabilityAuthority kind names.
# This is a one-way translation: plugin requests become shared-authority grants.
_PLUGIN_CAPABILITY_TO_KIND: dict[Capability, str] = {
    Capability.WORKSPACE_READ: "workspace-read",
    Capability.NAMED_EFFECT_APPEND: "workspace-write",
    Capability.EVENT_SUBSCRIBE: "process-run",  # event subscriptions use process-run scope
    Capability.EXTENSION_STATE: "workspace-write",  # extension state uses workspace-write scope
}


def plugin_capability_to_authority_kind(cap: Capability) -> str:
    """Translate a plugin Capability to a CapabilityAuthority kind name.

    Raises:
        PluginCapabilityDenied: if the capability has no registered authority kind.
    """
    if cap not in _PLUGIN_CAPABILITY_TO_KIND:
        raise PluginCapabilityDenied(
            cap.value,
            detail=f"plugin capability {cap.value!r} has no registered authority kind",
        )
    return _PLUGIN_CAPABILITY_TO_KIND[cap]


@dataclass(frozen=True, slots=True)
class CapabilityRequest:
    """One *request* for an effect class.  Carries no authority.

    A request is pure data.  It cannot be used to perform an effect; only
    the authority layer can turn a request into a ``CapabilityGrant``.
    """

    capability: Capability
    #: Optional human/operator justification recorded for the audit trail.
    justification: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.capability, Capability):
            raise PluginCapabilityDenied(
                str(self.capability),
                detail="capability name is not a member of the host capability enum",
            )


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    """One *issued* grant for an effect class.

    Grants are minted only by the authority layer (``GrantSet.grant``) and
    are handed to plugin code exclusively through the narrow facade.
    """

    capability: Capability
    plugin_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.capability, Capability):
            raise PluginCapabilityDenied(
                str(self.capability),
                plugin_id=self.plugin_id,
                detail="grant carries a non-enum capability",
            )


@dataclass(frozen=True, slots=True)
class CapabilityDenied:
    """An auditable denial record naming the capability that was missing."""

    capability: Capability
    plugin_id: str
    reason: str = "not granted"


class GrantSet:
    """The complete grant set for exactly one plugin.

    Default is **empty** — including for self-authored plugins.  There is no
    code path that populates this implicitly from authorship, file location,
    or any ambient property of the process.
    """

    __slots__ = ("_plugin_id", "_grants", "_mintable", "_live")

    def __init__(
        self,
        plugin_id: str,
        grants: frozenset[Capability] = frozenset(),
        *,
        mintable: bool = True,
        live: "Callable[[], bool] | None" = None,
    ) -> None:
        self._plugin_id = plugin_id
        self._grants: frozenset[Capability] = frozenset(grants)
        #: Whether this object may mint extended sets.  The host keeps the
        #: mintable copy; every reference handed to plugin code is marked
        #: non-mintable so ``grant``/``revoke`` cannot be reached from a
        #: plugin as an escalation path, even by convention.
        self._mintable = mintable
        #: Optional liveness predicate supplied by the host at activation.
        #: A dead activation makes every capability check on this view fail,
        #: so retained references cannot read or act after drain/disable/
        #: revoke — correctness never depends on the plugin dropping them.
        self._live = live

    def as_plugin_view(self) -> "GrantSet":
        """Return an equivalent **non-mintable** copy for plugin code."""
        return GrantSet(
            self._plugin_id, self._grants, mintable=False, live=self._live
        )

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def capabilities(self) -> frozenset[Capability]:
        """The granted set as seen right now.

        A dead activation sees an empty set: a retained view cannot even
        introspect authority it no longer holds.
        """
        if self._live is not None and not self._live():
            return frozenset()
        return self._grants

    def has(self, capability: Capability) -> bool:
        """True only when this plugin holds an explicit live grant."""
        if self._live is not None and not self._live():
            return False
        return capability in self._grants

    def require(self, capability: Capability) -> None:
        """Fail closed when the plugin lacks an explicit grant.

        Raises:
            PluginCapabilityDenied: always, when the grant is absent or the
                activation that minted this view is dead.  This is the
                load-bearing refusal and is never bypassed.
        """
        if self._live is not None and not self._live():
            raise PluginCapabilityDenied(
                capability.value,
                plugin_id=self._plugin_id,
                detail="this activation has been drained/revoked; "
                "the issued grant view is no longer live",
            )
        if capability not in self._grants:
            raise PluginCapabilityDenied(
                capability.value,
                plugin_id=self._plugin_id,
                detail="self-authored plugins start with no capabilities and operate "
                "only via explicit grants",
            )

    def deny(self, capability: Capability, *, reason: str = "not granted") -> CapabilityDenied:
        return CapabilityDenied(capability, self._plugin_id, reason)

    def grant(self, capability: Capability) -> "GrantSet":
        """Return a NEW GrantSet extended by one capability.

        This is the only minting point in the framework and it is explicit:
        a grant appears only because an authority-bearing caller asked for
        it by name.  GrantSets handed to plugin code are marked
        non-mintable (``as_plugin_view``), so this method raises there
        instead of merely producing a set the host ignores.
        """
        if not self._mintable:
            raise PluginCapabilityDenied(
                str(getattr(capability, "value", capability)),
                plugin_id=self._plugin_id,
                detail="grant sets held by plugin code cannot mint grants; "
                "only the host's authority copy may",
            )
        if not isinstance(capability, Capability):
            raise PluginCapabilityDenied(
                str(capability),
                plugin_id=self._plugin_id,
                detail="cannot grant a non-enum capability",
            )
        return GrantSet(self._plugin_id, self._grants | {capability})

    def revoke(self, capability: Capability) -> "GrantSet":
        """Return a NEW GrantSet with one capability removed."""
        if not self._mintable:
            raise PluginCapabilityDenied(
                str(getattr(capability, "value", capability)),
                plugin_id=self._plugin_id,
                detail="grant sets held by plugin code cannot mutate grants",
            )
        return GrantSet(self._plugin_id, self._grants - {capability})

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        names = sorted(item.value for item in self._grants)
        return f"GrantSet(plugin_id={self._plugin_id!r}, grants={names})"


def default_grants(plugin_id: str) -> GrantSet:
    """The grant set every plugin starts with: empty.

    Named explicitly so that "no ambient trust" is a single auditable call
    site rather than an implicit default scattered through the loader.
    """
    return GrantSet(plugin_id)


def validate_requests(requests: tuple[CapabilityRequest, ...]) -> None:
    """Reject any request whose capability is unknown to the host.

    Fails closed: unknown capability names refuse the load rather than
    being ignored silently.  A bare ``Capability`` is accepted as a request
    carrying no justification, so manifests may declare either form.
    """
    for request in requests:
        capability = getattr(request, "capability", request)
        if capability not in HOST_CAPABILITIES:
            raise PluginCapabilityDenied(
                str(capability),
                detail="capability is unknown to the host; open enums are refused",
            )
