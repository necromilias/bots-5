"""The narrow host facade — what a plugin may actually touch.

This is the load-bearing least-privilege boundary.  The host's
``AppStateStore`` Protocol spans roughly seventy methods across chats,
attachments, folders, the import queue, search index rebuild, window/dock/
keybinding/font settings and raw attachment bytes.  Handing that object to
an extension would grant all of it, which is precisely the ambient-authority
failure the campaign's locked decisions forbid.

Instead a plugin receives only the narrow seams below, and each seam is
gated on an explicit grant held in the plugin's ``GrantSet``.

Prohibitions enforced structurally (not by convention):

* no direct Qt access reaches plugin code;
* no global mutable access is handed to plugin code;
* no core SQLite connection reaches plugin code;
* no ``AppStateStore`` reference reaches plugin code — the facade holds it
  privately and never exposes it.

Every mutating seam takes or joins the existing effect-admission grant at
the callee boundary.  This module does not invent a second authority system.

When a shared ``CapabilityAuthority`` is configured, every seam that uses a
capability enters the authority's ``effect()`` boundary with the correct
``kind`` declared.  The authority decrements budget and appends an audit
entry for each effect.

Lifetime rule: every seam also checks a *liveness token*.  A facade handed
to a plugin belongs to one activation generation; when the host drains,
disables or revokes that plugin the token goes dead and previously issued
seams refuse further work.  Correctness therefore never depends on a plugin
dropping its references.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ContextManager

from .capabilities import Capability, GrantSet, plugin_capability_to_authority_kind
from .errors import PluginCapabilityDenied

try:
    from bots5.core.capabilities import CapabilityAuthority, CapabilityDenied
except ImportError:
    CapabilityAuthority = None  # type: ignore
    CapabilityDenied = None  # type: ignore


def _never_revoked() -> bool:
    """Default token predicate: a standalone facade is never invalidated.

    The host supplies a real predicate at activation so that drain, disable
    and revoke invalidate the surfaces they handed out.  A missing predicate
    is permissive only for objects the host never issued; anything created by
    ``PluginHost.activate`` is bound to the handle's live state.
    """
    return False


#: The closed receipt-outcome vocabulary.  ``UNKNOWN`` keeps the same
#: meaning it has across the campaign's authority substrate
#: (``data_root_authority._Claim.status == "UNKNOWN"``): an effect was
#: issued and its outcome is unestablished — never auto-retried, never
#: displayed as a clean outcome.  ``refused`` is a definite non-application
#: decided *before* the effect was issued; recording that as UNKNOWN would
#: be untruthful in the opposite direction.
EFFECT_OUTCOMES: frozenset[str] = frozenset(
    {"applied", "refused", "cancelled", "UNKNOWN"}
)


@dataclass(frozen=True, slots=True)
class EffectReceipt:
    """One truthful record of a bounded effect.

    Receipts represent success, refusal before issue, cancellation and
    terminal UNKNOWN.  A receipt never claims an effect that was not
    admitted, and never labels a pre-issue refusal as UNKNOWN.
    """

    effect_name: str
    outcome: str  # "applied" | "refused" | "cancelled" | "UNKNOWN"
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in EFFECT_OUTCOMES:
            raise PluginCapabilityDenied(
                Capability.NAMED_EFFECT_APPEND.value,
                plugin_id=self.effect_name,
                detail=f"unknown effect outcome {self.outcome!r}; "
                f"the receipt vocabulary is closed",
            )


class ReadOnlyQueryView:
    """Read-only namespaced query seam (``Capability.WORKSPACE_READ``).

    Exposes only bounded reads inside granted roots.  There is no write,
    delete or enumerate-everything path.

    The view additionally checks a liveness token supplied by the host so a
    view handed to a plugin stops working once that activation is drained,
    disabled or revoked.
    """

    __slots__ = ("_grants", "_roots", "_filesystem", "_revoked", "_spend")

    def __init__(
        self,
        grants: GrantSet,
        roots: Mapping[str, Path],
        *,
        filesystem=None,
        revoked: Callable[[], bool] | None = None,
        spend: Callable[[], ContextManager[None]] | None = None,
    ) -> None:
        self._grants = grants
        self._roots = dict(roots)
        self._filesystem = filesystem
        self._revoked = revoked or _never_revoked
        self._spend = spend or nullcontext

    @property
    def granted_roots(self) -> Mapping[str, Path]:
        return dict(self._roots)

    def _check_live(self) -> None:
        if self._revoked():
            raise PluginCapabilityDenied(
                Capability.WORKSPACE_READ.value,
                plugin_id=self._grants.plugin_id,
                detail="this activation has been drained/revoked; "
                "the issued view is no longer live",
            )

    def _resolve(self, root_name: str, relative: str) -> Path:
        """Resolve one relative path inside an explicitly granted root.

        Refuses traversal outside the root.  A missing root is a denial, not
        a fallback to the process working directory.

        Containment is checked on both the lexical path and the fully
        resolved path: ``Path.resolve()`` follows symlinks, so a link that
        points outside the granted root is refused even when its own name
        sits inside it.
        """
        self._grants.require(Capability.WORKSPACE_READ)
        self._check_live()
        if root_name not in self._roots:
            raise PluginCapabilityDenied(
                Capability.WORKSPACE_READ.value,
                plugin_id=self._grants.plugin_id,
                detail=f"no workspace root is granted under name {root_name!r}",
            )
        root = Path(self._roots[root_name]).resolve()
        candidate = (root / relative).resolve()
        for probe in ((root / relative), candidate):
            if probe != root and root not in probe.parents:
                raise PluginCapabilityDenied(
                    Capability.WORKSPACE_READ.value,
                    plugin_id=self._grants.plugin_id,
                    detail=f"path escapes the granted root {root_name!r}",
                )
        return candidate

    def read_text(self, root_name: str, relative: str) -> str:
        """Read one file inside a granted root, refusing traversal.

        When a shared authority is present, this read consumes one unit from
        the plugin's ``workspace-read`` grant through the authority's
        ``effect()`` boundary.
        """
        with self._spend():
            path = self._resolve(root_name, relative)
            if self._filesystem is not None:
                return self._filesystem.read_text(path)
            return path.read_text(encoding="utf-8")

    def list_names(self, root_name: str, relative: str = "") -> tuple[str, ...]:
        """List entry names directly inside a granted root subdirectory.

        Like ``read_text``, this consumes one unit from the shared authority.
        """
        with self._spend():
            path = self._resolve(root_name, relative)
            if self._filesystem is not None:
                return tuple(self._filesystem.list_names(path))
            if not path.is_dir():
                return ()
            return tuple(sorted(entry.name for entry in path.iterdir()))


class HostFacade:
    """The complete narrow surface handed to a plugin.

    Holds the wide store privately and never exposes it.  Every seam is
    grant-gated, and when a shared authority is configured each seam enters
    the authority's ``effect()`` boundary with the correct kind.
    """

    __slots__ = (
        "_grants",
        "_query",
        "_effects",
        "_state",
        "_admission",
        "_plugin_id",
        "_revoked",
        "_authority",
        "_authority_grants",
    )

    def __init__(
        self,
        grants: GrantSet,
        *,
        query: ReadOnlyQueryView | None = None,
        admission=None,
        effects=None,
        state=None,
        revoked: Callable[[], bool] | None = None,
        live: Callable[[], bool] | None = None,
        authority: "CapabilityAuthority | None" = None,
        authority_grants: "Mapping[str, object] | None" = None,
    ) -> None:
        # Plugin code never receives the host's mintable copy; the view also
        # carries the activation's liveness predicate so retained grant
        # references fail closed after drain/disable/revoke.
        if live is not None:
            grants = GrantSet(
                grants.plugin_id, grants.capabilities, mintable=False, live=live
            )
        else:
            grants = grants.as_plugin_view()
        self._grants = grants
        self._plugin_id = grants.plugin_id
        self._query = query
        self._admission = admission
        self._effects: list[EffectReceipt] = list(effects or ())
        self._state: dict[str, str] = dict(state or {})
        self._revoked = revoked or _never_revoked
        self._authority = authority  # Shared CapabilityAuthority for v0.2, or None
        self._authority_grants: dict[str, object] = dict(authority_grants or {})

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def grants(self) -> GrantSet:
        """The plugin's grants.  Inspectable; not amplifiable by the plugin."""
        return self._grants

    @contextmanager
    def _spend(self, capability: Capability) -> Iterator[None]:
        """Enter the shared authority's ``effect()`` boundary for ``capability``.

        Yields inside the authority's in-flight tracking so the effect is
        settled when the caller exits the context.  If no shared authority is
        configured (legacy path) this is a no-op context.

        Raises:
            PluginCapabilityDenied: if the authority denies (exhausted,
                expired, revoked, kind mismatch, etc.).
        """
        if self._authority is None:
            yield
            return

        kind = plugin_capability_to_authority_kind(capability)
        grant = self._authority_grants.get(kind)
        if grant is None:
            raise PluginCapabilityDenied(
                capability.value,
                plugin_id=self._plugin_id,
                detail=f"plugin has no shared-authority grant for kind {kind!r}",
            )
        try:
            with self._authority.effect(grant, units=1, kind=kind):
                yield
        except CapabilityDenied as exc:
            raise PluginCapabilityDenied(
                capability.value,
                plugin_id=self._plugin_id,
                detail=exc.message,
            ) from exc

    def _check_live(self, capability: Capability) -> None:
        if self._revoked():
            raise PluginCapabilityDenied(
                capability.value,
                plugin_id=self._plugin_id,
                detail="this activation has been drained/revoked; "
                "the issued facade is no longer live",
            )

    # -- seam: read-only query -------------------------------------------
    def query(self) -> ReadOnlyQueryView:
        """Return the read-only query seam, or refuse."""
        self._grants.require(Capability.WORKSPACE_READ)
        self._check_live(Capability.WORKSPACE_READ)
        if self._query is None:
            raise PluginCapabilityDenied(
                Capability.WORKSPACE_READ.value,
                plugin_id=self._plugin_id,
                detail="host supplied no workspace root for this plugin",
            )
        # Return a view that spends the shared workspace-read grant on each
        # actual read operation (read_text / list_names).
        return ReadOnlyQueryView(
            self._query._grants,
            self._query._roots,
            filesystem=self._query._filesystem,
            revoked=self._revoked,
            spend=lambda: self._spend(Capability.WORKSPACE_READ),
        )

    # -- seam: append-only named effect ----------------------------------
    def append_named_effect(self, name: str, payload: Mapping[str, object]) -> EffectReceipt:
        """Append one bounded, receipted named effect.

        Requires ``Capability.NAMED_EFFECT_APPEND``.  The effect is admitted
        through the *shared authority's* ``effect()`` boundary when the host
        supplied one, using the ``workspace-write`` kind; this facade never
        mints its own grant.
        """
        self._grants.require(Capability.NAMED_EFFECT_APPEND)
        self._check_live(Capability.NAMED_EFFECT_APPEND)
        if not name:
            raise PluginCapabilityDenied(
                Capability.NAMED_EFFECT_APPEND.value,
                plugin_id=self._plugin_id,
                detail="named effect requires a name",
            )
        try:
            with self._spend(Capability.NAMED_EFFECT_APPEND):
                with self._admit():
                    receipt = EffectReceipt(name, "applied")
        except Exception as exc:  # noqa: BLE001 - recorded truthfully below
            # The effect body never ran because admission itself refused, so
            # claiming UNKNOWN would be untruthful: this is a definite
            # non-application.  UNKNOWN stays reserved for outcomes that are
            # genuinely unestablished after an admitted effect was issued.
            receipt = EffectReceipt(name, "refused", f"effect not applied: {exc}")
        self._effects.append(receipt)
        return receipt

    @contextmanager
    def _admit(self) -> Iterator[None]:
        """Join the host's existing effect admission, if one was supplied.

        Absence of an admission callable is NOT permissive for mutations:
        the grant check has already happened, and the host is expected to
        bind real admission.  A host that supplies no admission gets no
        silent authority widening here.
        """
        if self._admission is None:
            yield
            return
        with self._admission():
            yield

    def effects(self) -> tuple[EffectReceipt, ...]:
        return tuple(self._effects)

    # -- seam: extension-scoped state ------------------------------------
    def extension_state(self) -> "ExtensionStateView":
        """Return the extension-scoped key/value seam, or refuse."""
        self._grants.require(Capability.EXTENSION_STATE)
        self._check_live(Capability.EXTENSION_STATE)
        return ExtensionStateView(
            self._grants,
            self._state,
            revoked=self._revoked,
            spend=lambda: self._spend(Capability.EXTENSION_STATE),
        )

    @property
    def _state_store(self) -> dict[str, str]:  # pragma: no cover - internal
        return self._state


class ExtensionStateView:
    """Extension-scoped key/value state (``Capability.EXTENSION_STATE``).

    Deliberately tiny: get/set/delete/keys over string values inside one
    plugin's own namespace.  No cross-plugin visibility, no SQL, no
    transaction control reaching plugin code.

    The view carries the same liveness token as its facade so a view kept
    past drain/disable/revoke stops working rather than continuing to read
    and write through a captured reference.  Each operation also spends one
    unit from the plugin's shared ``workspace-write`` grant when a shared
    authority is configured.
    """

    __slots__ = ("_grants", "_state", "_revoked", "_spend")

    def __init__(
        self,
        grants: GrantSet,
        state: dict[str, str],
        *,
        revoked: Callable[[], bool] | None = None,
        spend: Callable[[], ContextManager[None]] | None = None,
    ) -> None:
        self._grants = grants
        self._state = state
        self._revoked = revoked or _never_revoked
        self._spend = spend or nullcontext

    def _check(self) -> None:
        self._grants.require(Capability.EXTENSION_STATE)
        if self._revoked():
            raise PluginCapabilityDenied(
                Capability.EXTENSION_STATE.value,
                plugin_id=self._grants.plugin_id,
                detail="this activation has been drained/revoked; "
                "the issued state view is no longer live",
            )

    def get(self, key: str, default: str | None = None) -> str | None:
        with self._spend():
            self._check()
            return self._state.get(key, default)

    def set(self, key: str, value: str) -> None:
        with self._spend():
            self._check()
            if not isinstance(key, str) or not key:
                raise PluginCapabilityDenied(
                    Capability.EXTENSION_STATE.value,
                    plugin_id=self._grants.plugin_id,
                    detail="state key must be a non-empty string",
                )
            self._state[key] = value

    def delete(self, key: str) -> None:
        with self._spend():
            self._check()
            self._state.pop(key, None)

    def keys(self) -> tuple[str, ...]:
        with self._spend():
            self._check()
            return tuple(sorted(self._state))


@dataclass(frozen=True, slots=True)
class PluginContext:
    """The immutable bundle handed to a plugin at activation.

    A plugin receives this and nothing else.  It carries no store reference,
    no Qt object, no connection and no module-level escape hatch.

    ``grants`` is the same non-mintable view the facade holds; the mintable
    authority copy never leaves ``PluginHost``.
    """

    plugin_id: str
    facade: HostFacade
    grants: GrantSet

    @property
    def capabilities(self) -> Sequence[Capability]:
        return tuple(sorted(self.grants.capabilities, key=lambda item: item.value))
