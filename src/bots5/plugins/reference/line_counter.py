"""The one intentionally boring self-authored reference plugin.

It counts lines in files inside an explicitly granted workspace root.

This plugin is deliberately unremarkable on purpose: it exists to prove that
the *same* capability machinery used by tools and code execution is
consumable by a plugin, with no ambient trust and no special casing.

Crucially it is **self-authored** and still starts with zero capabilities.
Being authored in-tree grants it nothing: with no ``Capability.WORKSPACE_READ``
grant it refuses to read anything, and the refusal is typed and named.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..capabilities import Capability, CapabilityRequest
from ..errors import PluginCapabilityDenied
from ..facade import PluginContext
from ..manifest import PluginManifest

PLUGIN_ID = "bots5.plugin.reference.line_count"
PLUGIN_VERSION = "0.1.0"


def manifest() -> PluginManifest:
    """Return this plugin's manifest.

    The declared capability is a REQUEST.  Constructing this manifest does
    not grant the capability.
    """
    return PluginManifest(
        plugin_id=PLUGIN_ID,
        semantic_version=PLUGIN_VERSION,
        declared_capabilities=(
            CapabilityRequest(
                Capability.WORKSPACE_READ,
                "count lines inside an explicitly granted workspace root",
            ),
        ),
        display_name="Reference line counter",
        summary="Counts lines inside one explicitly granted workspace root.",
    )


@dataclass(frozen=True, slots=True)
class LineCountResult:
    """One boring, truthful result."""

    path: str
    lines: int

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "lines": self.lines}


def count_lines(context: PluginContext, root_name: str, relative: str) -> LineCountResult:
    """Count lines in one file.

    Raises:
        PluginCapabilityDenied: when the plugin holds no
            ``Capability.WORKSPACE_READ`` grant, or when the path escapes the
            granted root.  Denial is typed and names the capability; it is
            never a silent empty result.
    """
    view = context.facade.query()  # grant gate lives inside the facade seam
    text = view.read_text(root_name, relative)
    lines = len(text.splitlines())
    return LineCountResult(relative, lines)


def count_lines_all(context: PluginContext, root_name: str, relative: str = "") -> tuple[LineCountResult, ...]:
    """Count lines across the immediate entries of a granted root."""
    view = context.facade.query()
    names = view.list_names(root_name, relative)
    results: list[LineCountResult] = []
    for name in names:
        path = f"{relative}/{name}" if relative else name
        try:
            results.append(count_lines(context, root_name, path))
        except (PluginCapabilityDenied, IsADirectoryError, UnicodeDecodeError):
            # A directory or a non-text file is not an authority failure; it
            # is simply not countable.  Skipping it keeps the plugin boring
            # without weakening any refusal.
            continue
    return tuple(results)
