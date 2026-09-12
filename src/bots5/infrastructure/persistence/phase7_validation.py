"""Bounded and explicit expensive validation boundaries for Phase 7 search."""

from __future__ import annotations

from .phase7_schema import validate_phase7_schema


def validate_phase7_current(connection, *, require_fts: bool) -> None:
    """Validate startup facts without scanning the whole derived index."""
    validate_phase7_schema(connection, require_fts=require_fts, expensive=False)


def validate_phase7_rebuild(connection) -> None:
    """Run expensive FTS/key checks only at migration/rebuild/diagnostic boundaries."""
    validate_phase7_schema(connection, require_fts=True, expensive=True)

