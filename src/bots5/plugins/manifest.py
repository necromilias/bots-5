"""Plugin manifest: identity, declared capabilities and host negotiation.

The manifest FORMAT is host-owned.  A manifest is a *request*: nothing in it
grants authority, and no manifest field may assert its own trust level.

Validation is fail-closed:

* unknown fields refuse the load (never silently ignored);
* a namespace collision refuses the load;
* an unsatisfiable ``api_range`` marks the plugin incompatible (not loaded);
* a capability unknown to the host refuses the load;
* a schema violation refuses the load — there is no partially-trusted
  half-loaded plugin.

``trust_level`` is deliberately NOT a manifest field: trust derives from the
ratified policy, not self-declaration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .capabilities import Capability, CapabilityRequest, validate_requests
from .errors import PluginIncompatible, PluginManifestInvalid

#: Manifest schema version.  Independent of the application version, the
#: migration revision and any archive-format version.
MANIFEST_SCHEMA_VERSION = 1

#: The host API version this build implements.
HOST_API_VERSION = 1

#: Namespace reserved by the host.  Plugin ids outside it are refused.
HOST_NAMESPACE = "bots5.plugin"

#: Field names accepted in a raw manifest mapping.  Anything else fails closed.
_ALLOWED_FIELDS = frozenset(
    {
        "plugin_id",
        "semantic_version",
        "api_range",
        "manifest_schema_version",
        "declared_capabilities",
        "requested_surfaces",
        "absent_surface_policy",
        "entrypoint_kind",
        "event_interests",
        "display_name",
        "summary",
    }
)


class EntrypointKind(str, Enum):
    """How the host materialises the plugin body."""

    IN_PROCESS = "in_process"
    SUPERVISED_SUBPROCESS = "supervised_subprocess"
    DECLARATIVE = "declarative"


class PluginSurface(str, Enum):
    """Presentation surfaces a plugin may declare."""

    DESKTOP = "desktop"
    ANDROID = "android"
    HEADLESS = "headless"


class AbsentSurfacePolicy(str, Enum):
    """Declared (never defaulted) behaviour on a surface the plugin lacks."""

    LOGIC_ONLY = "logic_only"
    HIDE_PLUGIN_ON_SURFACE = "hide_plugin_on_surface"
    REFUSE_INSTALL = "refuse_install"


@dataclass(frozen=True, slots=True)
class HostApiRange:
    """Explicit min..max host API range — a range, not equality."""

    min_host_api: int = 1
    max_host_api: int = HOST_API_VERSION

    def __post_init__(self) -> None:
        if self.min_host_api < 1:
            raise PluginManifestInvalid("api_range.min_host_api must be >= 1")
        if self.max_host_api < self.min_host_api:
            raise PluginManifestInvalid(
                "api_range.max_host_api must be >= api_range.min_host_api"
            )

    def satisfied_by(self, host_api: int) -> bool:
        return self.min_host_api <= host_api <= self.max_host_api


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """A validated plugin manifest.

    Constructing one does not load or trust the plugin; it only establishes
    that the declared request is well-formed and negotiable.
    """

    plugin_id: str
    semantic_version: str
    #: Declared capabilities are REQUESTS.  Empty by default.
    declared_capabilities: tuple[CapabilityRequest, ...] = ()
    api_range: HostApiRange = field(default_factory=HostApiRange)
    manifest_schema_version: int = MANIFEST_SCHEMA_VERSION
    requested_surfaces: frozenset[PluginSurface] = frozenset({PluginSurface.HEADLESS})
    absent_surface_policy: AbsentSurfacePolicy = AbsentSurfacePolicy.LOGIC_ONLY
    entrypoint_kind: EntrypointKind = EntrypointKind.IN_PROCESS
    event_interests: tuple[str, ...] = ()
    display_name: str = ""
    summary: str = ""

    def __post_init__(self) -> None:
        _validate_plugin_id(self.plugin_id)
        if not self.semantic_version:
            raise PluginManifestInvalid("semantic_version must be non-empty")
        # Normalise bare Capability entries into CapabilityRequest so the
        # declared tuple is always homogeneous: a manifest may declare either
        # form, but what it stores is always a request.
        normalised = tuple(
            item
            if isinstance(item, CapabilityRequest)
            else CapabilityRequest(item)
            for item in self.declared_capabilities
        )
        object.__setattr__(self, "declared_capabilities", normalised)
        if self.manifest_schema_version != MANIFEST_SCHEMA_VERSION:
            raise PluginManifestInvalid(
                f"unsupported manifest_schema_version: {self.manifest_schema_version} "
                f"(host supports {MANIFEST_SCHEMA_VERSION}); no cross-version silent acceptance"
            )
        validate_requests(self.declared_capabilities)
        if not self.requested_surfaces:
            raise PluginManifestInvalid("requested_surfaces must not be empty")
        for surface in self.requested_surfaces:
            if not isinstance(surface, PluginSurface):
                raise PluginManifestInvalid(f"unknown surface: {surface!r}")
        if not isinstance(self.absent_surface_policy, AbsentSurfacePolicy):
            raise PluginManifestInvalid(
                f"unknown absent_surface_policy: {self.absent_surface_policy!r}"
            )
        if not isinstance(self.entrypoint_kind, EntrypointKind):
            raise PluginManifestInvalid(f"unknown entrypoint_kind: {self.entrypoint_kind!r}")

    @property
    def requested_capability_names(self) -> tuple[str, ...]:
        return tuple(request.capability.value for request in self.declared_capabilities)

    def as_raw_declaration(self) -> dict:
        """Return this manifest's declaration as a raw field mapping.

        Used by re-validation paths that must re-parse the declaration
        through ``PluginManifest.__post_init__`` rather than trusting a
        cached object.  The output round-trips through the dataclass
        constructor; it is not a trust shortcut.
        """
        return {
            "plugin_id": self.plugin_id,
            "semantic_version": self.semantic_version,
            "declared_capabilities": self.declared_capabilities,
            "api_range": self.api_range,
            "manifest_schema_version": self.manifest_schema_version,
            "requested_surfaces": self.requested_surfaces,
            "absent_surface_policy": self.absent_surface_policy,
            "entrypoint_kind": self.entrypoint_kind,
            "event_interests": self.event_interests,
            "display_name": self.display_name,
            "summary": self.summary,
        }

    def negotiate(self, host_api: int = HOST_API_VERSION) -> None:
        """Raise when the host cannot satisfy this manifest's api_range.

        An unsatisfiable range lists the plugin as *incompatible*; it is not
        loaded and no partial trust is established.
        """
        if not self.api_range.satisfied_by(host_api):
            raise PluginIncompatible(
                f"plugin {self.plugin_id} requires host api "
                f"{self.api_range.min_host_api}..{self.api_range.max_host_api} "
                f"but host provides {host_api}"
            )


def _validate_plugin_id(plugin_id: str) -> None:
    """Require the reserved namespace and a non-empty leaf."""
    if not isinstance(plugin_id, str) or not plugin_id:
        raise PluginManifestInvalid("plugin_id must be a non-empty string")
    prefix = f"{HOST_NAMESPACE}."
    if not plugin_id.startswith(prefix):
        raise PluginManifestInvalid(
            f"plugin_id must live in the reserved namespace {prefix!r}: {plugin_id!r}"
        )
    if len(plugin_id) == len(prefix):
        raise PluginManifestInvalid(f"plugin_id has an empty leaf: {plugin_id!r}")


def manifest_from_mapping(
    raw: dict, *, host_api: int = HOST_API_VERSION
) -> PluginManifest:
    """Parse and validate a raw manifest mapping.  Fails closed.

    Unknown keys refuse the load instead of being ignored silently, and the
    api_range is negotiated before the manifest is accepted.
    """
    if not isinstance(raw, dict):
        raise PluginManifestInvalid("manifest must be a mapping")

    unknown = sorted(set(raw) - _ALLOWED_FIELDS)
    if unknown:
        raise PluginManifestInvalid(
            "unknown manifest fields fail closed: " + ", ".join(unknown)
        )

    missing = sorted({"plugin_id", "semantic_version"} - set(raw))
    if missing:
        raise PluginManifestInvalid("missing required manifest fields: " + ", ".join(missing))

    raw_range = raw.get("api_range") or {}
    if not isinstance(raw_range, dict):
        raise PluginManifestInvalid("api_range must be a mapping")
    unknown_range = sorted(set(raw_range) - {"min_host_api", "max_host_api"})
    if unknown_range:
        raise PluginManifestInvalid(
            "unknown api_range fields fail closed: " + ", ".join(unknown_range)
        )

    try:
        api_range = HostApiRange(
            min_host_api=int(raw_range.get("min_host_api", 1)),
            max_host_api=int(raw_range.get("max_host_api", HOST_API_VERSION)),
        )
    except (TypeError, ValueError) as exc:
        raise PluginManifestInvalid(f"api_range is malformed: {exc}") from exc

    capabilities: list[CapabilityRequest] = []
    for item in raw.get("declared_capabilities", ()) or ():
        if isinstance(item, CapabilityRequest):
            capabilities.append(item)
            continue
        if isinstance(item, Capability):
            capabilities.append(CapabilityRequest(item))
            continue
        if isinstance(item, str):
            try:
                capabilities.append(CapabilityRequest(Capability(item)))
            except ValueError as exc:
                raise PluginManifestInvalid(
                    f"unknown capability {item!r}: the host capability enum is closed"
                ) from exc
            continue
        if isinstance(item, dict):
            name = item.get("capability") or item.get("name")
            if name is None:
                raise PluginManifestInvalid("capability entry is missing 'capability'")
            unknown_cap = sorted(set(item) - {"capability", "justification"})
            if unknown_cap:
                raise PluginManifestInvalid(
                    "unknown capability fields fail closed: " + ", ".join(unknown_cap)
                )
            try:
                capabilities.append(
                    CapabilityRequest(
                        Capability(name), str(item.get("justification", ""))
                    )
                )
            except ValueError as exc:
                raise PluginManifestInvalid(
                    f"unknown capability {name!r}: the host capability enum is closed"
                ) from exc
            continue
        raise PluginManifestInvalid(f"unsupported capability entry: {item!r}")

    surfaces: set[PluginSurface] = set()
    for item in raw.get("requested_surfaces", ("headless",)) or ():
        try:
            surfaces.add(PluginSurface(item))
        except ValueError as exc:
            raise PluginManifestInvalid(f"unknown surface {item!r}") from exc

    try:
        policy = AbsentSurfacePolicy(raw.get("absent_surface_policy", "logic_only"))
    except ValueError as exc:
        raise PluginManifestInvalid(
            f"unknown absent_surface_policy {raw.get('absent_surface_policy')!r}"
        ) from exc

    try:
        entrypoint = EntrypointKind(raw.get("entrypoint_kind", "in_process"))
    except ValueError as exc:
        raise PluginManifestInvalid(
            f"unknown entrypoint_kind {raw.get('entrypoint_kind')!r}"
        ) from exc

    manifest = PluginManifest(
        plugin_id=str(raw["plugin_id"]),
        semantic_version=str(raw["semantic_version"]),
        declared_capabilities=tuple(capabilities),
        api_range=api_range,
        manifest_schema_version=int(
            raw.get("manifest_schema_version", MANIFEST_SCHEMA_VERSION)
        ),
        requested_surfaces=frozenset(surfaces),
        absent_surface_policy=policy,
        entrypoint_kind=entrypoint,
        event_interests=tuple(str(item) for item in raw.get("event_interests", ()) or ()),
        display_name=str(raw.get("display_name", "")),
        summary=str(raw.get("summary", "")),
    )
    manifest.negotiate(host_api)
    return manifest
