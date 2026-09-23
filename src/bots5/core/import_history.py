"""Typed immutable imported-history projections.

These values intentionally keep source identity and local derivation separate;
they contain no provider handles, paths, endpoints, or payload locations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SourceKind(StrEnum):
    NATIVE = "native"
    V1_BOOTSTRAP = "v1-bootstrap"
    IMPORTED = "imported"


@dataclass(frozen=True, slots=True)
class ContinuationReadiness:
    chat_id: str
    base_key: str
    source_configuration: dict[str, object]
    resolution: str
    resolution_evidence: dict[str, object]
    choice_revision: int
    local_connection_id: str | None = None
    local_model_entry_id: str | None = None
    explicit_settings: dict[str, object] | None = None
    excluded_refs: tuple[dict[str, object], ...] = ()
    requirements: tuple[ContinuationRequirement, ...] = ()


@dataclass(frozen=True, slots=True)
class ContinuationRequirement:
    anchor_key: str
    ordinal: int
    source_imported_attempt_id: str | None
    source_native_attempt_id: str | None
    expected_digest: str
    expected_size: int
    representation_digest: str | None
    binding_kind: str
    imported_ref_id: str | None
    native_attachment_id: str | None
    blocked_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SourceHop:
    archive_id: str
    archive_version: int
    logical_content_digest: str
    object_id: str
    imported_at: str

    def __post_init__(self) -> None:
        if not self.archive_id or not self.object_id or self.archive_version not in {1, 2} or len(self.logical_content_digest) != 64:
            raise ValueError("source hop is malformed")


@dataclass(frozen=True, slots=True)
class ObjectProvenance:
    source_kind: SourceKind
    immediate: SourceHop | None
    prior_chain: tuple[SourceHop, ...] = ()
    derived_from_message_id: str | None = None

    def __post_init__(self) -> None:
        if self.source_kind is SourceKind.NATIVE and (self.immediate is not None or self.prior_chain):
            raise ValueError("native object cannot claim imported provenance")
        if self.source_kind is SourceKind.V1_BOOTSTRAP and (self.immediate is None or self.immediate.archive_version != 1 or self.prior_chain):
            raise ValueError("v1 bootstrap provenance is malformed")
        if self.source_kind is SourceKind.IMPORTED and self.immediate is None:
            raise ValueError("imported object lacks immediate source provenance")
        triples = [(hop.archive_id, hop.logical_content_digest, hop.object_id) for hop in ((self.immediate,) if self.immediate is not None else ()) + self.prior_chain]
        if len(triples) != len(set(triples)):
            raise ValueError("source provenance repeats a hop")


def imported_provenance(*, archive_id: str, archive_version: int, logical_content_digest: str, source_object_id: str, imported_at: str, source: ObjectProvenance | None) -> ObjectProvenance:
    """Add exactly one outer hop without laundering local derivation/source IDs."""
    hop = SourceHop(archive_id, archive_version, logical_content_digest, source_object_id, imported_at)
    if source is None or source.source_kind is SourceKind.NATIVE:
        return ObjectProvenance(SourceKind.V1_BOOTSTRAP if archive_version == 1 else SourceKind.IMPORTED, hop)
    if source.immediate is None:
        raise ValueError("non-native source lacks its immediate hop")
    return ObjectProvenance(SourceKind.IMPORTED, hop, (source.immediate, *source.prior_chain), source.derived_from_message_id)
