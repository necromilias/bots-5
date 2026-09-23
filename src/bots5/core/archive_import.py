"""Pure pre-write Archive-import planning primitives."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import codecs
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from uuid6 import uuid7

from bots5.core.interchange import canonical_json_bytes, parse_jsonl, strict_json_loads
from bots5.infrastructure.archive_package import ArchivePackageError, ArchiveUnsupportedError, validate_archive


class ImportErrorCode(ValueError):
    """Closed safe intake failure codes."""


SOURCE_CHANGED = "SOURCE_CHANGED"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
SOURCE_OUTSIDE_RESOLVER = "SOURCE_OUTSIDE_RESOLVER"
ARCHIVE_INVALID = "ARCHIVE_INVALID"
UNSUPPORTED = "UNSUPPORTED"
RESOLVER_UNAVAILABLE = "RESOLVER_UNAVAILABLE"
RESOLUTION_CANCELLED = "RESOLUTION_CANCELLED"
RESOURCE_LIMIT = "RESOURCE_LIMIT"


@dataclass(frozen=True, slots=True)
class SourceBinding:
    device: int
    inode: int
    byte_size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ArchiveSourcePlan:
    """Facts extracted only from the immutable bytes which passed validation."""

    archive_id: str
    archive_version: int
    logical_content_digest: str
    source_chat_id: str
    source_chat_revision: int
    binding: SourceBinding
    plan: "ImportPlan"
    payloads: tuple[tuple[str, object], ...]
    verified_ready_payloads: tuple[tuple[str, int, object], ...] = ()


@dataclass(slots=True)
class VerifiedPayloadSnapshot:
    """Anonymous verified payload bytes owned until import settlement."""
    digest: str
    size: int
    handle: object
    text_representation_id: bytes | None
    ineligibility_reason: str | None

    def close(self) -> None:
        self.handle.close()


def _require_snapshot_capacity(handle: object, additional_bytes: int) -> None:
    """Refuse a scratch write before its retained anonymous bytes are created."""
    try:
        status = os.fstatvfs(handle.fileno())
    except OSError as exc:
        raise ImportErrorCode(RESOURCE_LIMIT) from exc
    unit = status.f_frsize or status.f_bsize
    if unit <= 0 or status.f_bavail * unit < additional_bytes + unit:
        raise ImportErrorCode(RESOURCE_LIMIT)


def _classify_sealed_snapshot(handle: object, digest: str) -> tuple[bytes | None, str | None]:
    """Boundedly derive native text eligibility without materializing payload bytes."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    contains_nul = False
    invalid_utf8 = False
    try:
        handle.seek(0)
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            if not invalid_utf8:
                try:
                    decoded = decoder.decode(block)
                    contains_nul = contains_nul or "\0" in decoded
                except UnicodeDecodeError:
                    invalid_utf8 = True
        if not invalid_utf8:
            try:
                decoded = decoder.decode(b"", final=True)
                contains_nul = contains_nul or "\0" in decoded
            except UnicodeDecodeError:
                invalid_utf8 = True
    finally:
        handle.seek(0)
    if invalid_utf8:
        return None, "invalid_utf8"
    if contains_nul:
        return None, "contains_nul"
    return bytes.fromhex(digest), None


def _snapshot_bytes(digest: str, raw: bytes) -> VerifiedPayloadSnapshot:
    handle = tempfile.TemporaryFile(mode="w+b")
    try:
        _require_snapshot_capacity(handle, len(raw))
        for offset in range(0, len(raw), 1024 * 1024):
            handle.write(raw[offset:offset + 1024 * 1024])
        handle.seek(0)
        try:
            raw.decode("utf-8", errors="strict")
            ineligibility = "contains_nul" if b"\0" in raw else None
        except UnicodeDecodeError:
            ineligibility = "invalid_utf8"
        return VerifiedPayloadSnapshot(
            digest, len(raw), handle,
            None if ineligibility is not None else bytes.fromhex(digest), ineligibility,
        )
    except BaseException:
        handle.close()
        raise


def _snapshot_member(
    package, member: str, digest: str, size: int, *,
    cancelled: Callable[[], bool] | None = None,
) -> VerifiedPayloadSnapshot:
    handle = tempfile.TemporaryFile(mode="w+b")
    actual = hashlib.sha256()
    total = 0
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    contains_nul = False
    invalid_utf8 = False
    try:
        _require_snapshot_capacity(handle, size)
        with package.open(member, "r") as stream:
            while True:
                if cancelled is not None and cancelled():
                    raise ImportErrorCode(RESOLUTION_CANCELLED)
                block = stream.read(1024 * 1024)
                if not block:
                    break
                handle.write(block); actual.update(block); total += len(block)
                if not invalid_utf8:
                    try:
                        decoded = decoder.decode(block)
                        contains_nul = contains_nul or "\0" in decoded
                    except UnicodeDecodeError:
                        invalid_utf8 = True
            if not invalid_utf8:
                try:
                    decoded = decoder.decode(b"", final=True)
                    contains_nul = contains_nul or "\0" in decoded
                except UnicodeDecodeError:
                    invalid_utf8 = True
        if total != size or actual.hexdigest() != digest:
            raise ImportErrorCode(ARCHIVE_INVALID)
        handle.seek(0)
        return VerifiedPayloadSnapshot(
            digest, size, handle,
            None if (invalid_utf8 or contains_nul) else bytes.fromhex(digest),
            "invalid_utf8" if invalid_utf8 else "contains_nul" if contains_nul else None,
        )
    except BaseException:
        handle.close()
        raise


def close_payload_snapshots(captured: ArchiveSourcePlan) -> None:
    for _, payload in captured.payloads:
        payload.close()


@dataclass(frozen=True, slots=True)
class PlannedIdentity:
    """One fresh local identity paired with opaque source evidence."""

    object_kind: str
    source_id: str
    local_id: str


@dataclass(frozen=True, slots=True)
class ImportPlan:
    """A sealed pre-cutoff graph plan; it never retains an intake pathname."""

    identities: tuple[PlannedIdentity, ...]
    graph_inventory: tuple[dict[str, str], ...]
    graph_plan_sha256: bytes
    chat: dict[str, object]
    messages: tuple[dict[str, object], ...]
    attempts: tuple[dict[str, object], ...]
    attachments: tuple[dict[str, object], ...]
    context_plans: tuple[dict[str, object], ...]
    message_attachment_relations: tuple[dict[str, object], ...]
    attempt_attachment_relations: tuple[dict[str, object], ...]
    chat_configuration: dict[str, object]
    archive_provenance: dict[str, object]
    manifest: dict[str, object]
    source_binding: dict[str, object]
    object_provenance: tuple[dict[str, object], ...]
    provenance_node_ids: tuple[dict[str, object], ...]
    continuation_history: dict[str, object] | None
    history_bindings: tuple[dict[str, object], ...]
    # Receiver-local automatic equivalence is not portable archive evidence.
    # It is nevertheless an import-time fact, so preflight seals it into the
    # internal journal plan before the durable cutoff.
    initial_receiver_continuation: tuple[dict[str, object], ...] = ()

    def local_id(self, object_kind: str, source_id: str) -> str:
        for identity in self.identities:
            if identity.object_kind == object_kind and identity.source_id == source_id:
                return identity.local_id
        raise ImportErrorCode(ARCHIVE_INVALID)


_OBJECT_ORDER = {"chat": 0, "message": 1, "lineage": 2, "attachment": 3, "attempt": 4}


def _object_rows(
    chat: dict[str, object],
    messages: tuple[dict[str, object], ...],
    attempts: tuple[dict[str, object], ...],
    attachments: tuple[dict[str, object], ...],
) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = [("chat", str(chat["source_id"]))]
    values.extend(("message", str(row["source_id"])) for row in messages)
    # Several revisions may legitimately share a lineage.  It is one
    # identity-bearing object kind, so the map contains it once; duplicate
    # source message/attempt/attachment IDs remain invalid below.
    values.extend(("lineage", str(row["lineage_id"])) for row in messages)
    values.extend(("attachment", str(row["source_id"])) for row in attachments)
    values.extend(("attempt", str(row["source_id"])) for row in attempts)
    unique = tuple(dict.fromkeys(values))
    for kind in ("chat", "message", "attachment", "attempt"):
        scoped = [source for row_kind, source in values if row_kind == kind]
        if len(scoped) != len(set(scoped)):
            raise ImportErrorCode(ARCHIVE_INVALID)
    return tuple(sorted(unique, key=lambda item: (_OBJECT_ORDER[item[0]], item[1])))


def _sealed_plan_payload(plan: ImportPlan) -> dict[str, object]:
    """Return every fact covered by an import plan's pre-cutoff grant."""
    return {
        "inventory": list(plan.graph_inventory),
        "chat": plan.chat,
        "messages": list(plan.messages),
        "attempts": list(plan.attempts),
        "attachments": list(plan.attachments),
        "context_plans": list(plan.context_plans),
        "message_attachment_relations": list(plan.message_attachment_relations),
        "attempt_attachment_relations": list(plan.attempt_attachment_relations),
        "chat_configuration": plan.chat_configuration,
        "archive_provenance": plan.archive_provenance,
        "manifest": plan.manifest,
        "source_binding": plan.source_binding,
        "object_provenance": list(plan.object_provenance),
        "provenance_node_ids": list(plan.provenance_node_ids),
        "continuation_history": plan.continuation_history,
        "history_bindings": list(plan.history_bindings),
        "initial_receiver_continuation": list(plan.initial_receiver_continuation),
    }


def with_initial_receiver_continuation(
    plan: ImportPlan, rows: tuple[dict[str, object], ...],
) -> ImportPlan:
    """Seal deterministic receiver facts discovered during preflight.

    These rows are deliberately internal: archive provenance remains the
    source archive's immutable evidence, while receiver selection is admitted
    exactly once against the pre-cutoff local catalogue snapshot.
    """
    planned = replace(plan, initial_receiver_continuation=rows)
    return replace(
        planned,
        graph_plan_sha256=hashlib.sha256(canonical_json_bytes(_sealed_plan_payload(planned))).digest(),
    )


def _build_plan(
    *,
    chat: dict[str, object],
    messages: tuple[dict[str, object], ...],
    attempts: tuple[dict[str, object], ...],
    attachments: tuple[dict[str, object], ...],
    context_plans: tuple[dict[str, object], ...],
    message_attachment_relations: tuple[dict[str, object], ...],
    attempt_attachment_relations: tuple[dict[str, object], ...],
    chat_configuration: dict[str, object],
    archive_provenance: dict[str, object],
    manifest: dict[str, object],
    source_binding: dict[str, object],
    object_provenance: tuple[dict[str, object], ...],
    continuation_history: dict[str, object] | None,
    history_bindings: tuple[dict[str, object], ...],
) -> ImportPlan:
    identities = tuple(
        PlannedIdentity(kind, source_id, str(uuid7()))
        for kind, source_id in _object_rows(chat, messages, attempts, attachments)
    )
    inventory = tuple(
        {"object_kind": item.object_kind, "local_id": item.local_id, "source_id": item.source_id}
        for item in identities
    )
    # Archive v1 has no per-object wire provenance.  Its validated graph is
    # still canonicalized into the same complete identity inventory; the
    # durable importer records the actual v1 archive as its first bootstrap
    # hop when it commits this plan.
    if not object_provenance:
        object_provenance = tuple(
            {
                "object_kind": item.object_kind,
                "object_id": item.source_id,
                "source": {"kind": "v1-bootstrap", "immediate": None, "prior_chain": []},
                "derivation": {"kind": "root", "predecessor": None},
            }
            for item in identities
        )
    provenance_by_identity = {
        (str(row["object_kind"]), str(row["object_id"])): row
        for row in object_provenance
    }
    provenance_node_ids: list[dict[str, object]] = []
    for identity in identities:
        source = provenance_by_identity[(identity.object_kind, identity.source_id)]["source"]
        hops = [] if source.get("immediate") is None else [source["immediate"]]
        hops.extend(source.get("prior_chain", ()))
        for ordinal in range(len(hops) + 1):
            provenance_node_ids.append({
                "object_kind": identity.object_kind,
                "source_id": identity.source_id,
                "ordinal": ordinal,
                "node_id": str(uuid7()),
            })
    # The recovery inventory deliberately records only identities.  The grant
    # hash additionally seals every exact source row and link payload that a
    # later graph transaction may translate through those identities.
    plan = ImportPlan(
        identities=identities,
        graph_inventory=inventory,
        graph_plan_sha256=b"",
        chat=chat,
        messages=messages,
        attempts=attempts,
        attachments=attachments,
        context_plans=context_plans,
        message_attachment_relations=message_attachment_relations,
        attempt_attachment_relations=attempt_attachment_relations,
        chat_configuration=chat_configuration,
        archive_provenance=archive_provenance,
        manifest=manifest,
        source_binding=source_binding,
        object_provenance=object_provenance,
        provenance_node_ids=tuple(provenance_node_ids),
        continuation_history=continuation_history,
        history_bindings=history_bindings,
    )
    return replace(
        plan,
        graph_plan_sha256=hashlib.sha256(canonical_json_bytes(_sealed_plan_payload(plan))).digest(),
    )


def resolve_source(path: Path, resolver_roots: tuple[Path, ...]) -> Path:
    """Validate the explicit intake path and optional external resolver roots."""
    if not path.is_absolute():
        raise ImportErrorCode(SOURCE_OUTSIDE_RESOLVER)
    try:
        resolved = path.resolve(strict=True)
        roots = tuple(root.resolve(strict=True) for root in resolver_roots)
    except OSError as exc:
        raise ImportErrorCode(SOURCE_UNAVAILABLE) from exc
    if roots and not any(resolved.is_relative_to(root) for root in roots):
        raise ImportErrorCode(SOURCE_OUTSIDE_RESOLVER)
    return path


def source_fingerprint(path: Path) -> tuple[int, int, int, int, int]:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ImportErrorCode(SOURCE_UNAVAILABLE) from exc
    if not stat.S_ISREG(value.st_mode) or value.st_size < 0:
        raise ImportErrorCode(SOURCE_UNAVAILABLE)
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _capture_snapshot(
    path: Path, expected: tuple[int, int, int, int, int], *, chunk_size: int,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[SourceBinding, object]:
    """Capture one no-follow immutable unlinked snapshot with bounded chunks."""
    if chunk_size < 4096:
        raise ValueError("capture chunk is too small")
    if source_fingerprint(path) != expected:
        raise ImportErrorCode(SOURCE_CHANGED)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ImportErrorCode(SOURCE_UNAVAILABLE) from exc
    try:
        observed = os.fstat(fd)
        actual = (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns)
        if not stat.S_ISREG(observed.st_mode) or actual != expected:
            raise ImportErrorCode(SOURCE_CHANGED)
        digest = hashlib.sha256()
        with tempfile.TemporaryFile(mode="w+b") as scratch:
            _require_snapshot_capacity(scratch, observed.st_size)
            remaining = observed.st_size
            while remaining:
                if cancelled is not None and cancelled():
                    raise ImportErrorCode(RESOLUTION_CANCELLED)
                block = os.read(fd, min(chunk_size, remaining))
                if not block:
                    raise ImportErrorCode(SOURCE_CHANGED)
                scratch.write(block); digest.update(block); remaining -= len(block)
            if os.read(fd, 1):
                raise ImportErrorCode(SOURCE_CHANGED)
            after = os.fstat(fd)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != actual:
                raise ImportErrorCode(SOURCE_CHANGED)
            scratch.seek(0)
            # Transfer a separate live handle; the context's handle closes,
            # while the anonymous file has no reusable pathname.
            duplicate = os.dup(scratch.fileno())
            return SourceBinding(*actual, digest.hexdigest()), os.fdopen(duplicate, "w+b")
    finally:
        os.close(fd)


def capture_source(path: Path, expected: tuple[int, int, int, int, int], *, chunk_size: int = 1024 * 1024) -> tuple[SourceBinding, bytes]:
    """Compatibility helper for small pure tests; production uses snapshots."""
    binding, snapshot = _capture_snapshot(path, expected, chunk_size=chunk_size)
    try:
        return binding, snapshot.read()
    finally:
        snapshot.close()


def _v1_safe_context_history_bindings(
    attempts: list[dict[str, object]], messages: list[dict[str, object]],
    attachments: list[dict[str, object]], attempt_attachment_relations: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Canonicalize validated v1 request-time context into v2 binding rows.

    The safe context is immutable source evidence.  Current bindings are a
    separate typed map into the exporting graph and are emitted only when the
    source ID and the validated source row establish that exact relationship.
    """
    messages_by_id = {str(row["source_id"]): row for row in messages}
    attachments_by_id = {str(row["source_id"]): row for row in attachments}
    selected_by_attempt: dict[str, set[str]] = {}
    for relation in attempt_attachment_relations:
        selected_by_attempt.setdefault(str(relation["attempt_id"]), set()).add(
            str(relation["attachment_id"])
        )
    result: list[dict[str, object]] = []
    for attempt in attempts:
        attempt_id = str(attempt["source_id"])
        provenance = attempt.get("request_time_provenance")
        if not isinstance(provenance, dict) or (
            provenance.get("status") != "available" or provenance.get("snapshot_version") != 3
        ):
            continue
        context = provenance.get("context")
        sources = context.get("sources") if isinstance(context, dict) else None
        if not isinstance(sources, list):
            raise ImportErrorCode(ARCHIVE_INVALID)
        for ordinal, source in enumerate(sources):
            if not isinstance(source, dict):
                raise ImportErrorCode(ARCHIVE_INVALID)
            kind = source.get("kind")
            source_id = source.get("source_id")
            status = source.get("source_id_status")
            if not isinstance(kind, str) or not isinstance(source_id, str) or status not in {"available", "redacted"}:
                raise ImportErrorCode(ARCHIVE_INVALID)
            if kind == "attachment":
                evidence_digest = source.get("representation_digest")
                digest_kind = "representation"
            else:
                projection = {
                    key: source.get(key) for key in (
                        "kind", "role", "content", "state", "eligible", "selected",
                        "selection_reason", "representation_id", "representation_digest",
                    )
                }
                evidence_digest = hashlib.sha256(canonical_json_bytes(projection)).hexdigest()
                digest_kind = "safe-source-record"
            if not isinstance(evidence_digest, str) or len(evidence_digest) != 64:
                raise ImportErrorCode(ARCHIVE_INVALID)
            current_binding: dict[str, str] | None = None
            if status == "available":
                if kind == "attachment" and (
                    source_id in selected_by_attempt.get(attempt_id, set())
                    and source_id in attachments_by_id
                ):
                    current_binding = {"object_kind": "attachment", "object_id": source_id}
                elif kind == "current_user":
                    message = messages_by_id.get(str(attempt.get("user_message_id")))
                    if message is not None and source_id == str(attempt.get("user_message_id")) and source.get("content") == message.get("content"):
                        current_binding = {"object_kind": "message", "object_id": source_id}
                elif kind == "history":
                    message = messages_by_id.get(source_id)
                    if message is not None and source.get("role") == message.get("role") and source.get("content") == message.get("content"):
                        current_binding = {"object_kind": "message", "object_id": source_id}
            result.append({
                "attempt_id": attempt_id, "binding_kind": "context-source", "ordinal": ordinal,
                "snapshot_source": {
                    "kind": kind, "source_id": source_id, "source_id_status": status,
                    "evidence_digest": evidence_digest, "digest_kind": digest_kind,
                },
                "current_binding": current_binding,
                "binding_state": "bound" if current_binding is not None else (
                    "historical-only" if kind == "bots_instruction" else "unavailable"
                ),
            })
    return result


def capture_validated_archive(
    path: Path,
    expected: tuple[int, int, int, int, int],
    *,
    cancelled: Callable[[], bool] | None = None,
) -> ArchiveSourcePlan:
    """Validate the one captured byte sequence; the pathname is never reopened."""
    binding, snapshot = _capture_snapshot(
        path, expected, chunk_size=1024 * 1024, cancelled=cancelled,
    )
    payloads: list[tuple[str, VerifiedPayloadSnapshot]] = []
    try:
        validated = validate_archive(snapshot)
        # validate_archive has closed the ZIP container.  This small metadata
        # read is from that exact in-memory sequence, never the intake path.
        import zipfile
        snapshot.seek(0)
        with zipfile.ZipFile(snapshot, "r") as package:
            manifest = strict_json_loads(package.read("manifest.json"))
            chat = strict_json_loads(package.read("domain/chat.json"))
            messages = parse_jsonl(package.read("domain/messages.jsonl"))
            attempts = parse_jsonl(package.read("domain/attempts.jsonl"))
            attachments = parse_jsonl(package.read("domain/attachments.jsonl"))
            for item in attachments:
                if item.get("integrity_status") == "verified" and f"payloads/sha256/{item.get('blob_digest')}" in package.namelist():
                    digest = str(item["blob_digest"])
                    payloads.append((digest, _snapshot_member(
                        package, f"payloads/sha256/{digest}", digest, int(item["byte_size"]),
                        cancelled=cancelled,
                    )))
            context_plans = parse_jsonl(package.read("domain/context-plans.jsonl"))
            message_attachment_relations = parse_jsonl(package.read("domain/message-attachments.jsonl"))
            attempt_attachment_relations = parse_jsonl(package.read("domain/attempt-attachments.jsonl"))
            chat_configuration = strict_json_loads(package.read("domain/chat-configuration.json"))
            archive_provenance = strict_json_loads(package.read("domain/provenance.json"))
            if manifest.get("archive_version") == 2:
                object_provenance = parse_jsonl(package.read("domain/object-provenance.jsonl"))
                continuation_history = strict_json_loads(package.read("domain/continuation-history.json"))
                history_bindings = parse_jsonl(package.read("domain/history-bindings.jsonl"))
            else:
                object_provenance = []
                continuation_history = None
                history_bindings = _v1_safe_context_history_bindings(
                    attempts, messages, attachments, attempt_attachment_relations,
                )
    except ImportErrorCode:
        for _, payload in payloads:
            payload.close()
        raise
    except ArchiveUnsupportedError as exc:
        for _, payload in payloads:
            payload.close()
        raise ImportErrorCode(UNSUPPORTED) from exc
    except (ArchivePackageError, OSError, ValueError, KeyError) as exc:
        for _, payload in payloads:
            payload.close()
        raise ImportErrorCode(ARCHIVE_INVALID) from exc
    finally:
        snapshot.close()
    if (
        type(manifest) is not dict
        or type(chat) is not dict
        or type(manifest.get("archive_version")) is not int
        or manifest["archive_version"] not in {1, 2}
        or type(chat.get("source_id")) is not str
        or not chat["source_id"]
        or type(chat_configuration) is not dict
        or type(archive_provenance) is not dict
        or any(type(row) is not dict for row in (*messages, *attempts, *attachments, *context_plans, *message_attachment_relations, *attempt_attachment_relations, *object_provenance, *history_bindings))
        or (continuation_history is not None and type(continuation_history) is not dict)
    ):
        raise ImportErrorCode(ARCHIVE_INVALID)
    revision = chat.get("revision", 0)
    if type(revision) is not int or revision < 0:
        raise ImportErrorCode(ARCHIVE_INVALID)
    plan = _build_plan(
        chat=chat,
        messages=tuple(messages),
        attempts=tuple(attempts),
        attachments=tuple(attachments),
        context_plans=tuple(context_plans),
        message_attachment_relations=tuple(message_attachment_relations),
        attempt_attachment_relations=tuple(attempt_attachment_relations),
        chat_configuration=chat_configuration,
        archive_provenance=archive_provenance,
        manifest=manifest,
        source_binding={
            "device": binding.device, "inode": binding.inode,
            "byte_size": binding.byte_size, "mtime_ns": binding.mtime_ns,
            "ctime_ns": binding.ctime_ns, "sha256": binding.sha256,
        },
        object_provenance=tuple(object_provenance),
        continuation_history=continuation_history,
        history_bindings=tuple(history_bindings),
    )
    return ArchiveSourcePlan(
        archive_id=validated.archive_id,
        archive_version=manifest["archive_version"],
        logical_content_digest=validated.logical_content_digest,
        source_chat_id=chat["source_id"],
        source_chat_revision=revision,
        binding=binding,
        plan=plan,
        payloads=tuple(payloads),
    )


def resolve_external_payloads(
    captured: ArchiveSourcePlan,
    resolver_roots: tuple[Path, ...],
    *,
    cancelled: Callable[[], bool] | None = None,
) -> ArchiveSourcePlan:
    """Resolve external attachment bytes once, before the durable cutoff.

    The Archive wire intentionally carries digests and sizes rather than
    filesystem locations.  A resolver therefore walks only the operator's
    roots, snapshots each plausible regular file through the same no-follow
    capture used for the archive, and retains bytes only after their declared
    digest has been verified.  It never records a candidate name or reopens a
    candidate after preflight.
    """
    expected: dict[str, int] = {}
    external_policy = captured.plan.manifest.get("attachment_policy") == "external-reference"
    for attachment in captured.plan.attachments:
        digest = attachment.get("blob_digest")
        size = attachment.get("byte_size")
        availability = attachment.get("payload_availability")
        if type(digest) is not str or type(size) is not int or size < 0:
            raise ImportErrorCode(ARCHIVE_INVALID)
        # Export-time verification never promises that an independently
        # supplied receiver still has the file.  Every externally referenced
        # attachment is therefore eligible for one resolver pass, regardless
        # of the source availability label.  Absence remains a successful,
        # truthful MISSING_EXTERNAL result; only an embedded package's absent
        # declared member is a wire-validation error.
        if (external_policy or availability == "missing-external") and digest not in dict(captured.payloads):
            prior = expected.setdefault(digest, size)
            if prior != size:
                raise ImportErrorCode(ARCHIVE_INVALID)
    if not expected:
        return captured
    if not resolver_roots:
        # A payload-free archive has no resolver dependency.  Once external
        # bytes are actually required, an empty operator root set remains an
        # honest unresolved resolver condition.
        raise ImportErrorCode(RESOLVER_UNAVAILABLE)
    resolved: dict[str, object] = dict(captured.payloads)
    wanted_sizes = set(expected.values())
    try:
        def walk_error(error: OSError) -> None:
            raise ImportErrorCode(RESOLVER_UNAVAILABLE) from error

        for configured_root in resolver_roots:
            root = configured_root.resolve(strict=True)
            if not root.is_dir():
                raise OSError("resolver root is not a directory")
            for directory, _directories, files in os.walk(
                root, topdown=True, followlinks=False, onerror=walk_error,
            ):
                if cancelled is not None and cancelled():
                    raise ImportErrorCode(RESOLUTION_CANCELLED)
                for name in files:
                    if cancelled is not None and cancelled():
                        raise ImportErrorCode(RESOLUTION_CANCELLED)
                    candidate = Path(directory, name)
                    try:
                        fingerprint = source_fingerprint(candidate)
                    except ImportErrorCode as exc:
                        # A disappearing candidate is an I/O failure, not
                        # evidence that the declared object is absent.
                        raise ImportErrorCode(RESOLVER_UNAVAILABLE) from exc
                    if fingerprint[2] not in wanted_sizes:
                        continue
                    binding, snapshot = _capture_snapshot(
                        candidate, fingerprint, chunk_size=1024 * 1024,
                        cancelled=cancelled,
                    )
                    digest = binding.sha256
                    if digest in expected and expected[digest] == binding.byte_size:
                        prior = resolved.get(digest)
                        if prior is None:
                            representation_id, ineligibility_reason = _classify_sealed_snapshot(snapshot, digest)
                            resolved[digest] = VerifiedPayloadSnapshot(
                                digest, binding.byte_size, snapshot,
                                representation_id, ineligibility_reason,
                            )
                        else:
                            snapshot.close()
                        if expected.keys() <= resolved.keys():
                            break
                    else:
                        snapshot.close()
                if expected.keys() <= resolved.keys():
                    break
            if expected.keys() <= resolved.keys():
                break
    except ImportErrorCode:
        for value in resolved.values():
            if isinstance(value, VerifiedPayloadSnapshot):
                value.close()
        raise
    except OSError as exc:
        for value in resolved.values():
            if isinstance(value, VerifiedPayloadSnapshot):
                value.close()
        raise ImportErrorCode(RESOLVER_UNAVAILABLE) from exc
    payloads = tuple(sorted(resolved.items()))
    return ArchiveSourcePlan(
        archive_id=captured.archive_id,
        archive_version=captured.archive_version,
        logical_content_digest=captured.logical_content_digest,
        source_chat_id=captured.source_chat_id,
        source_chat_revision=captured.source_chat_revision,
        binding=captured.binding,
        plan=captured.plan,
        payloads=payloads,
    )
