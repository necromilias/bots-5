"""Self-authored reference plugins.

Membership in this package confers no trust and no capabilities.  These
plugins exist to exercise the same capability machinery as every other
consumer.
"""

from __future__ import annotations

from .line_counter import (
    PLUGIN_ID,
    PLUGIN_VERSION,
    LineCountResult,
    count_lines,
    count_lines_all,
    manifest,
)

__all__ = [
    "PLUGIN_ID",
    "PLUGIN_VERSION",
    "LineCountResult",
    "count_lines",
    "count_lines_all",
    "manifest",
]
