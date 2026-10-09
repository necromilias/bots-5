"""Plugin lifecycle: discovery, validation, grant binding, activation, drain.

State machine (mirrors the sealed research PLG-C05)::

    ABSENT -> DISCOVERED -> VALIDATED -> GRANT_REQUESTED -> ADMITTED
           -> ACTIVE -> DRAINING -> DISABLED -> REVOKED/PURGED

Invariants enforced here:

* Terminal states are **monotonic**: a revoked or disabled plugin cannot
  reactivate without re-passing VALIDATED and GRANT_REQUESTED.
* Disable never deletes data; revoke drains then disables; purge is a
  separate deliberate act.
* No transition may leave a plugin holding an issued effect grant — drain
  precedes terminal publication.
* Correctness never depends on garbage collection or destruction timing:
  event subscriptions are closed explicitly through ``PluginHandle.close``.
* When a shared ``CapabilityAuthority`` is configured, every capability-bound
  plugin effect is admitted through that authority's ``effect()`` boundary
  and revocation invalidates the shared grant (CLEANUP/REVOKED semantics).

Crash recovery is intentionally not guessed: an interrupted transition is
reconciled by the host at startup, never fabricated.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator

from .capabilities import Capability, CapabilityRequest, GrantSet, default_grants, plugin_capability_to_authority_kind
from .errors import PluginCapabilityDenied, PluginLifecycleError, PluginManifestInvalid
from .facade import HostFacade, PluginContext, ReadOnlyQueryView
from .manifest import HOST_API_VERSION, PluginManifest

try:
    from bots5.core.capabilities import CapabilityAuthority, CapabilityDenied, DirectoryScope, Subject, GrantRequest
except ImportError:
    # For tests that don't have CapabilityAuthority available
    CapabilityAuthority = None  # type: ignore
    CapabilityDenied = None  # type: ignore
    DirectoryScope = None  # type: ignore
    Subject = None  # type: ignore
    GrantRequest = None  # type: ignore

#: Minimum host API version this lifecycle implementation requires.
LIFECYCLE_HOST_API = HOST_API_VERSION


class LifecycleState(str, Enum):
    """The plugin lifecycle state machine."""

    ABSENT = "ABSENT"
    DISCOVERED = "DISCOVERED"
    VALIDATED = "VALIDATED"
    GRANT_REQUESTED = "GRANT_REQUESTED"
    ADMITTED = "ADMITTED"
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    DISABLED = "DISABLED"
    REVOKED = "REVOKED"
    PURGED = "PURGED"


#: States from which no forward transition is possible without re-validation.
TERMINAL_STATES: frozenset[LifecycleState] = frozenset(
    {LifecycleState.REVOKED, LifecycleState.PURGED}
)

#: States that require a fresh VALIDATED pass before reactivation.
_REVALIDATION_REQUIRED: frozenset[LifecycleState] = frozenset(
    {LifecycleState.DISABLED, LifecycleState.REVOKED, LifecycleState.PURGED}
)

#: States from which a terminal transition must be preceded by DRAINING —
#: i.e. subscriptions closed and the live activation invalidated first.
_REQUIRES_DRAIN_BEFORE_TERMINAL: frozenset[LifecycleState] = frozenset(
    {LifecycleState.ADMITTED, LifecycleState.ACTIVE}
)

_LEGAL_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.ABSENT: frozenset({LifecycleState.DISCOVERED}),
    LifecycleState.DISCOVERED: frozenset(
        {LifecycleState.VALIDATED, LifecycleState.DISABLED}
    ),
    LifecycleState.VALIDATED: frozenset(
        {LifecycleState.GRANT_REQUESTED, LifecycleState.ADMITTED, LifecycleState.DISABLED}
    ),
    LifecycleState.GRANT_REQUESTED: frozenset(
        {LifecycleState.ADMITTED, LifecycleState.DISABLED}
    ),
    LifecycleState.ADMITTED: frozenset(
        {LifecycleState.ACTIVE, LifecycleState.DRAINING, LifecycleState.DISABLED}
    ),
    LifecycleState.ACTIVE: frozenset({LifecycleState.DRAINING, LifecycleState.DISABLED}),
    LifecycleState.DRAINING: frozenset(
        {LifecycleState.DISABLED, LifecycleState.REVOKED}
    ),
    LifecycleState.DISABLED: frozenset(
        {LifecycleState.VALIDATED, LifecycleState.PURGED}
    ),
    # Re-validation is permitted after revocation, but only by re-passing
    # VALIDATED -> GRANT_REQUESTED: the grants were dropped and must be
    # re-issued explicitly.  PURGED alone is truly terminal.
    LifecycleState.REVOKED: frozenset(
        {LifecycleState.VALIDATED, LifecycleState.PURGED}
    ),
    LifecycleState.PURGED: frozenset(),
}


@dataclass(slots=True)
class PluginHandle:
    """One plugin's live runtime record.

    Owns the plugin's grant set and any event subscriptions, and closes
    them explicitly.  Subscriptions are never left to the garbage collector.

    ``activation_epoch`` numbers every activation of this plugin.  Facades
    created by an activation carry their epoch; once the handle leaves
    ACTIVE (drain, disable, revoke) or is re-activated, older epochs are
    dead and the surfaces they handed out refuse further work.  This makes
    "no GC-dependent correctness" real rather than aspirational: a plugin
    that keeps a stale facade reference gets refusals, not authority.
    """

    manifest: PluginManifest
    state: LifecycleState = LifecycleState.ABSENT
    grants: GrantSet = field(default_factory=lambda: default_grants(""))
    #: Explicitly-registered closeables (event subscriptions etc).
    _subscriptions: list = field(default_factory=list)
    _closed: bool = False
    #: Monotonic counter of activations for this handle.
    activation_epoch: int = 0

    @property
    def plugin_id(self) -> str:
        return self.manifest.plugin_id

    @property
    def closed(self) -> bool:
        return self._closed

    def is_live(self, epoch: int) -> bool:
        """True only while ACTIVE and still on the same activation epoch."""
        return self.state is LifecycleState.ACTIVE and epoch == self.activation_epoch

    def register_subscription(self, subscription) -> None:
        """Register one closeable subscription for explicit teardown."""
        if self._closed:
            raise PluginLifecycleError(
                f"plugin {self.plugin_id} is closed; cannot register subscriptions"
            )
        self._subscriptions.append(subscription)

    def subscriptions(self) -> tuple:
        return tuple(self._subscriptions)

    def close_subscriptions(self) -> int:
        """Close every subscription explicitly.  Returns the count closed.

        Errors from individual closes are collected and re-raised as a
        lifecycle error so a failed close is never silently swallowed.
        """
        failures: list[str] = []
        closed = 0
        for subscription in self._subscriptions:
            try:
                subscription.close()
                closed += 1
            except Exception as exc:  # noqa: BLE001 - surfaced below
                failures.append(f"{type(subscription).__name__}: {exc}")
        self._subscriptions.clear()
        if failures:
            raise PluginLifecycleError(
                f"plugin {self.plugin_id} subscription close failed: " + "; ".join(failures)
            )
        return closed

    def close(self) -> int:
        """Close the handle and all its subscriptions.  Idempotent."""
        if self._closed:
            return 0
        self._closed = True
        return self.close_subscriptions()


class PluginHost:
    """Owns plugin discovery, validation, grant binding and lifecycle.

    The host is the only component that may mint grants, and it does so only
    when an authority-bearing caller supplies them explicitly by name.
    Self-authorship is never a grant source.

    When constructed with a CapabilityAuthority, the host delegates grant
    minting to the shared authority and routes every plugin effect through
    the authority's ``effect()`` boundary.  Without a CapabilityAuthority it
    falls back to its own internal grants dict (legacy tests).
    """

    __slots__ = (
        "_host_api",
        "_handles",
        "_granted",
        "_admission",
        "_roots",
        "_events",
        "_capability_authority",
        "_authority_grants",  # plugin_id -> {kind: authority_grant_id}
    )

    def __init__(
        self,
        *,
        host_api: int = LIFECYCLE_HOST_API,
        admission=None,
        roots=None,
        events=None,
        capability_authority: "CapabilityAuthority | None" = None,
    ) -> None:
        self._host_api = host_api
        self._handles: dict[str, PluginHandle] = {}
        #: plugin_id -> explicitly granted capability set (the authority record)
        self._granted: dict[str, frozenset[Capability]] = {}
        self._admission = admission
        self._roots = dict(roots or {})
        #: Optional host EventBus for the explicit event-subscription seam.
        #: The bus is never handed to plugin code; only subscriptions are.
        self._events = events
        #: Shared CapabilityAuthority for v0.2 composition, or None for legacy
        self._capability_authority = capability_authority
        #: plugin_id -> {authority_kind: authority_grant_id}
        self._authority_grants: dict[str, dict[str, str]] = {}

    # -- discovery / validation -----------------------------------------
    def discover(self, manifest: PluginManifest) -> PluginHandle:
        """Parse and register a manifest.  Refuses namespace collisions.

        Discovery establishes no trust and issues no grants.
        """
        if not isinstance(manifest, PluginManifest):
            raise PluginManifestInvalid("discover requires a validated PluginManifest")
        if manifest.plugin_id in self._handles:
            raise PluginManifestInvalid(
                f"namespace collision on plugin_id {manifest.plugin_id!r}: refuse to load"
            )
        handle = PluginHandle(
            manifest=manifest,
            state=LifecycleState.DISCOVERED,
            grants=default_grants(manifest.plugin_id),
        )
        self._handles[manifest.plugin_id] = handle
        return handle

    def validate(self, plugin_id: str) -> PluginHandle:
        """Validate schema, api_range and capability-name closure.

        Re-validation after DISABLED / REVOKED re-parses the manifest from
        its raw declaration rather than trusting the cached object, so a
        reactivated plugin must satisfy the current host rules again.
        """
        handle = self._require(plugin_id)
        if handle.state in _REVALIDATION_REQUIRED:
            # Revalidation re-establishes trust from scratch: the previously
            # validated manifest is re-parsed through the same fail-closed
            # path a fresh load uses.  This blocks stale or drifted
            # declarations from riding a cached object back into service.
            PluginManifest(**handle.manifest.as_raw_declaration())
        self._transition(handle, LifecycleState.VALIDATED)
        handle.manifest.negotiate(self._host_api)
        return handle

    def request_grants(self, plugin_id: str) -> tuple[CapabilityRequest, ...]:
        """Publish the plugin's declared capability REQUESTS.

        This is a request, never a grant: nothing here increases what the
        plugin may do.
        """
        handle = self._require(plugin_id)
        self._transition(handle, LifecycleState.GRANT_REQUESTED)
        return handle.manifest.declared_capabilities

    def _ensure_capability_authority(self) -> CapabilityAuthority:
        """Require that CapabilityAuthority was provided.

        Raises:
            PluginLifecycleError: if no CapabilityAuthority was supplied.
        """
        if self._capability_authority is None:
            raise PluginLifecycleError(
                "PluginHost was not constructed with a CapabilityAuthority; "
                "bind_grants requires a shared authority for v0.2 composition"
            )
        return self._capability_authority

    def _directory_scope_for_plugin(self) -> "DirectoryScope | None":
        """Return the workspace DirectoryScope when roots are configured."""
        if not self._roots:
            return None
        return DirectoryScope(root=next(iter(self._roots.values())))

    def _create_authority_grants(
        self, plugin_id: str, capabilities: frozenset[Capability]
    ) -> dict[str, str]:
        """Mint one shared-authority grant per capability kind required.

        Returns a mapping from authority kind name to the issued grant id.
        A plugin may therefore hold multiple shared grants (e.g. one
        ``workspace-read`` grant and one ``workspace-write`` grant), each
        bound to the correct kind so that ``effect(kind=...)`` refuses a
        mismatched use.
        """
        authority = self._ensure_capability_authority()
        subject = Subject(kind="plugin", identity=plugin_id)
        scope = self._directory_scope_for_plugin()

        kinds: set[str] = set()
        for cap in capabilities:
            kinds.add(plugin_capability_to_authority_kind(cap))

        mapping: dict[str, str] = {}
        for kind in sorted(kinds):
            # DirectoryScope is only meaningful for workspace-bound kinds.
            request_scope = scope if kind in ("workspace-read", "workspace-write", "process-run") else None
            request = GrantRequest(
                subject=subject,
                kind=kind,
                scope=request_scope,
                ttl_seconds=3600.0,
                max_effects=1000,
            )
            grant = authority.grant(request)
            mapping[kind] = grant.grant_id
        return mapping

    def _revoke_authority_grants(self, plugin_id: str, reason: str) -> None:
        """Revoke every shared-authority grant held for this plugin.

        In-flight effects are tracked by the authority's ``effect()``
        context manager; revocation moves a grant to CLEANUP while effects
        are in flight and to REVOKED once they settle.  Calling this method
        more than once is idempotent on the authority side.
        """
        if self._capability_authority is None:
            return
        mapping = self._authority_grants.pop(plugin_id, {})
        for grant_id in mapping.values():
            grant = self._capability_authority._grants.get(grant_id)
            if grant is not None:
                self._capability_authority.revoke(grant, reason)

    @contextmanager
    def _spend_authority_effect(self, plugin_id: str, capability: Capability):
        """Return a context manager that spends one unit on the shared authority.

        Raises:
            PluginCapabilityDenied: when the authority denies.
        """
        if self._capability_authority is None:
            yield
            return

        kind = plugin_capability_to_authority_kind(capability)
        mapping = self._authority_grants.get(plugin_id, {})
        grant_id = mapping.get(kind)
        if not grant_id:
            raise PluginCapabilityDenied(
                capability.value,
                plugin_id=plugin_id,
                detail=f"plugin has no shared-authority grant for kind {kind!r}",
            )
        grant = self._capability_authority._grants.get(grant_id)
        if grant is None:
            raise PluginCapabilityDenied(
                capability.value,
                plugin_id=plugin_id,
                detail="shared-authority grant not found",
            )
        try:
            with self._capability_authority.effect(grant, units=1, kind=kind):
                yield
        except CapabilityDenied as exc:
            raise PluginCapabilityDenied(
                capability.value,
                plugin_id=plugin_id,
                detail=exc.message,
            ) from exc

    # -- grant binding (the only minting point) -------------------------
    def bind_grants(
        self, plugin_id: str, capabilities: frozenset[Capability] | set[Capability] | tuple[Capability, ...]
    ) -> GrantSet:
        """Explicitly grant capabilities by name.  The only minting point.

        A capability appears in the plugin's grant set only because an
        authority-bearing caller named it here.  There is no path that
        infers grants from self-authorship, file location or process state.

        When a shared CapabilityAuthority is present, grants are minted
        through it and checked via the authority at activation time.
        Without a CapabilityAuthority, the host's internal grants dict is used.
        """
        handle = self._require(plugin_id)
        self._transition(handle, LifecycleState.GRANT_REQUESTED)

        granted = frozenset(capabilities)
        unknown = sorted(
            item.value if isinstance(item, Capability) else str(item)
            for item in granted
            if not isinstance(item, Capability)
        )
        if unknown:
            raise PluginManifestInvalid(
                "cannot grant unknown capabilities: " + ", ".join(unknown)
            )

        # If CapabilityAuthority is available, delegate grant minting to it.
        if self._capability_authority is not None:
            self._authority_grants[plugin_id] = self._create_authority_grants(
                plugin_id, granted
            )
            # Keep the internal record for legacy inspection paths.
            self._granted[plugin_id] = granted
        else:
            # Legacy path: use internal grants dict only.
            self._granted[plugin_id] = granted

        grants = GrantSet(plugin_id, granted)
        handle.grants = grants
        return grants

    def admit(self, plugin_id: str) -> PluginHandle:
        """Move a granted plugin to ADMITTED."""
        handle = self._require(plugin_id)
        self._transition(handle, LifecycleState.ADMITTED)
        return handle

    # -- event subscriptions (explicit close, never GC-dependent) ---------
    def open_event_subscription(self, plugin_id: str, bus=None):
        """Subscribe an ACTIVE plugin to host events through ``bus``.

        This is the *integration* seam behind the documented limitation that
        ``event.subscribe`` had no wired bus.  Rules:

        * requires an ACTIVE plugin holding an explicit
          ``Capability.EVENT_SUBSCRIBE`` grant — declaring it in a manifest
          is a request and buys nothing here;
        * the subscription is registered on the handle, so drain/disable/
          revoke close it explicitly; correctness never depends on garbage
          collection or destruction timing;
        * the bus is not handed to plugin code as a long-lived object — only
          the returned subscription is, and it stops working when the
          activation dies;
        * if the host was constructed with ``events=`` and no per-call bus is
          given, the host bus is used.  When a shared authority is present,
          the subscription also consumes one unit from the plugin's
          ``process-run`` grant through the ONE shared authority.
        """
        handle = self._require(plugin_id)
        if handle.state is not LifecycleState.ACTIVE:
            raise PluginLifecycleError(
                f"plugin {plugin_id} is {handle.state.value}; "
                "event subscription requires an ACTIVE plugin"
            )
        handle.grants.require(Capability.EVENT_SUBSCRIBE)
        target = bus if bus is not None else self._events
        if target is None:
            raise PluginLifecycleError(
                "no event bus available: construct PluginHost(events=...) or "
                "pass one to open_event_subscription"
            )
        subscribe = getattr(target, "subscribe", None)
        if subscribe is None:
            raise PluginLifecycleError("event bus exposes no subscribe()")

        with self._spend_authority_effect(plugin_id, Capability.EVENT_SUBSCRIBE):
            subscription = subscribe()
            handle.register_subscription(subscription)
        return subscription

    # -- activation ------------------------------------------------------
    def _check_authority_grant(self, plugin_id: str, grants: GrantSet) -> None:
        """Check with CapabilityAuthority that every required grant is usable.

        Raises:
            PluginLifecycleError: if the authority denies any grant.
        """
        if self._capability_authority is None:
            return  # Legacy path: no authority to check

        mapping = self._authority_grants.get(plugin_id, {})
        if not mapping and not grants.capabilities:
            return

        required_kinds = {
            plugin_capability_to_authority_kind(cap) for cap in grants.capabilities
        }
        missing = required_kinds - set(mapping.keys())
        if missing:
            raise PluginLifecycleError(
                f"plugin {plugin_id!r} is missing shared-authority grants for kinds: "
                f"{sorted(missing)}"
            )

        for kind, grant_id in mapping.items():
            authority_grant = self._capability_authority._grants.get(grant_id)
            if authority_grant is None:
                raise PluginLifecycleError(
                    f"plugin {plugin_id!r} authority grant {grant_id!r} not found"
                )
            usable, reason, msg = self._capability_authority._check_usable(authority_grant)
            if not usable:
                raise PluginLifecycleError(
                    f"plugin {plugin_id!r} authority grant ({kind}) is not usable: {msg}"
                )

    def activate(self, plugin_id: str) -> PluginContext:
        """Hand the plugin its narrow facade.  Grants are already fixed.

        The returned surfaces carry this activation's epoch; once the plugin
        leaves ACTIVE or is re-activated they refuse further work, so a
        stale reference is never an authority leak.

        Activation always runs the full documented sequence from the
        current non-active state — VALIDATED → GRANT_REQUESTED → ADMITTED →
        ACTIVE (or DISABLED/REVOKED → VALIDATED first).  A caller cannot
        jump into admission without passing the grant-request step, and a
        plugin that *requested* capabilities in its manifest must have them
        explicitly bound (possibly as an empty set) before it activates.

        When a shared CapabilityAuthority is configured, activation also
        verifies the plugin's authority grants are still usable before
        proceeding, and the handed-out facade carries those grants so every
        effect is admitted through the shared authority's ``effect()``
        boundary with the correct kind.
        """
        handle = self._require(plugin_id)
        # Check with CapabilityAuthority if one is configured
        self._check_authority_grant(plugin_id, handle.grants)
        if handle.state not in (LifecycleState.ADMITTED, LifecycleState.ACTIVE):
            # Revocation is never silently reversible: REVOKED and PURGED
            # must re-enter through an explicit validate() call, which drops
            # the grants, rather than being revived as a side effect of
            # activate().  Monotonic terminality is load-bearing.
            if handle.state is LifecycleState.REVOKED or handle.state in TERMINAL_STATES:
                raise PluginLifecycleError(
                    f"illegal plugin transition {handle.state.value} -> ACTIVE for "
                    f"{plugin_id}; terminal states are monotonic and require an "
                    "explicit validate() re-entry before reactivation"
                )
            # Direct activation is a convenience for the zero-grant default
            # path only.  A plugin that *declared* capabilities must have
            # them explicitly bound (possibly as an empty set) and must pass
            # through admit() — there is no implicit fast lane from
            # VALIDATED to ACTIVE for capability-requesting plugins.
            if handle.manifest.declared_capabilities:
                if handle.state not in (
                    LifecycleState.VALIDATED,
                    LifecycleState.GRANT_REQUESTED,
                ):
                    raise PluginLifecycleError(
                        f"illegal plugin transition {handle.state.value} -> "
                        f"ADMITTED for {plugin_id}; declared-capability "
                        "plugins must validate first"
                    )
                if handle.state is LifecycleState.VALIDATED:
                    self._transition(handle, LifecycleState.GRANT_REQUESTED)
                missing = sorted(
                    request.capability.value
                    for request in handle.manifest.declared_capabilities
                    if not handle.grants.has(request.capability)
                )
                if missing:
                    raise PluginLifecycleError(
                        f"plugin {plugin_id} declares capabilities "
                        f"({', '.join(missing)}) but they were never bound by "
                        "bind_grants; a declaration is a request, never a grant"
                    )
                self._transition(handle, LifecycleState.ADMITTED)
            else:
                self._transition(handle, LifecycleState.ADMITTED)
        self._transition(handle, LifecycleState.ACTIVE)
        handle.activation_epoch += 1
        epoch = handle.activation_epoch
        live = lambda: handle.is_live(epoch)  # noqa: E731 - one-line predicate
        query = None
        if self._roots:
            query = ReadOnlyQueryView(
                GrantSet(
                    handle.grants.plugin_id,
                    handle.grants.capabilities,
                    mintable=False,
                    live=live,
                ),
                self._roots,
                revoked=lambda: not live(),
            )

        authority_grants: dict[str, object] = {}
        if self._capability_authority is not None:
            mapping = self._authority_grants.get(plugin_id, {})
            authority_grants = {
                kind: self._capability_authority._grants[grant_id]
                for kind, grant_id in mapping.items()
                if grant_id in self._capability_authority._grants
            }

        facade = HostFacade(
            handle.grants,
            query=query,
            admission=self._admission,
            revoked=lambda: not live(),
            live=live,
            authority=self._capability_authority,
            authority_grants=authority_grants,
        )
        return PluginContext(
            plugin_id=plugin_id, facade=facade, grants=facade.grants
        )

    # -- drain / terminal -------------------------------------------------
    def drain(self, plugin_id: str) -> int:
        """Begin DRAINING and close subscriptions explicitly.

        New admission closes immediately; unrelated already-admitted work may
        settle before a monotonic terminal state is published.  No plugin is
        left holding an issued effect grant.  When a shared authority is
        present, the plugin's shared grants are revoked here so in-flight
        effects settle through CLEANUP/REVOKED.
        """
        handle = self._require(plugin_id)
        self._transition(handle, LifecycleState.DRAINING)
        self._revoke_authority_grants(plugin_id, "plugin drain")
        return handle.close_subscriptions()

    def disable(self, plugin_id: str) -> PluginHandle:
        """Disable a plugin.  Never deletes data.

        Disabling an ACTIVE plugin drains it first: subscriptions are closed
        and the activation epoch dies before DISABLED is published, so there
        is no path from ACTIVE to a terminal-ish state that skips drain.
        """
        handle = self._require(plugin_id)
        if handle.state in _REQUIRES_DRAIN_BEFORE_TERMINAL:
            self._transition(handle, LifecycleState.DRAINING)
            self._revoke_authority_grants(plugin_id, "plugin disable")
            handle.close_subscriptions()
        self._transition(handle, LifecycleState.DISABLED)
        handle.grants = default_grants(plugin_id)
        self._granted.pop(plugin_id, None)
        self._authority_grants.pop(plugin_id, None)
        handle.close()
        return handle

    def revoke(self, plugin_id: str) -> PluginHandle:
        """Drain then revoke.  Revocation drops the grants and invalidates
        every shared-authority grant held for this plugin.

        In-flight effects are allowed to settle per the authority's
        deferred RELEASED/REVOKED semantics: a grant moves to CLEANUP while
        effects are in flight and becomes REVOKED once the last in-flight
        effect settles.
        """
        handle = self._require(plugin_id)
        if handle.state in _REQUIRES_DRAIN_BEFORE_TERMINAL:
            self._transition(handle, LifecycleState.DRAINING)
            self._revoke_authority_grants(plugin_id, "plugin revoke")
            handle.close_subscriptions()
        self._transition(handle, LifecycleState.REVOKED)
        handle.grants = default_grants(plugin_id)
        self._granted.pop(plugin_id, None)
        self._authority_grants.pop(plugin_id, None)
        handle.close()
        return handle

    def purge(self, plugin_id: str) -> None:
        """Purge is a separate deliberate act (never implicit).

        Purging an admitted or active plugin drains it and disables it first
        so no live activation survives into the terminal PURGED state.
        """
        handle = self._require(plugin_id)
        if handle.state in _REQUIRES_DRAIN_BEFORE_TERMINAL:
            self._transition(handle, LifecycleState.DRAINING)
            self._revoke_authority_grants(plugin_id, "plugin purge")
            handle.close_subscriptions()
            self._transition(handle, LifecycleState.DISABLED)
        self._transition(handle, LifecycleState.PURGED)
        self._granted.pop(plugin_id, None)
        self._authority_grants.pop(plugin_id, None)
        handle.close()
        del self._handles[plugin_id]

    # -- inspection --------------------------------------------------------
    def handle(self, plugin_id: str) -> PluginHandle | None:
        return self._handles.get(plugin_id)

    def state_of(self, plugin_id: str) -> LifecycleState:
        handle = self._require(plugin_id)
        return handle.state

    def grants_of(self, plugin_id: str) -> GrantSet:
        handle = self._require(plugin_id)
        return handle.grants

    # -- internals ----------------------------------------------------------
    def _require(self, plugin_id: str) -> PluginHandle:
        handle = self._handles.get(plugin_id)
        if handle is None:
            raise PluginLifecycleError(f"unknown plugin: {plugin_id!r}")
        return handle

    def _transition(self, handle: PluginHandle, target: LifecycleState) -> None:
        """Enforce the legal transition graph and monotonic terminality."""
        current = handle.state
        if target not in _LEGAL_TRANSITIONS[current]:
            extra = ""
            if current in TERMINAL_STATES:
                extra = "; terminal states are monotonic and require re-validation"
            raise PluginLifecycleError(
                f"illegal plugin transition {current.value} -> {target.value} "
                f"for {handle.plugin_id}{extra}"
            )
        if current in _REVALIDATION_REQUIRED and target is LifecycleState.VALIDATED:
            # Reactivation is permitted but must re-pass grant binding.
            handle.grants = default_grants(handle.plugin_id)
            self._granted.pop(handle.plugin_id, None)
            self._authority_grants.pop(handle.plugin_id, None)
            # A fresh validation cycle starts with a fresh subscription
            # surface; the closed marker from the previous terminal pass
            # must not block newly registered closeables.
            handle._closed = False
        handle.state = target
