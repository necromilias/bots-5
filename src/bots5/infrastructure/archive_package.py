"""Strict, streaming Archive v1 ZIP64 package boundary.

This module has no database knowledge.  Core supplies canonical logical bytes;
this module only packages or rejects an untrusted container.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import stat
import struct
import tempfile
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import BinaryIO, Iterator, Mapping

from bots5.core.export import (
    ArchiveLogicalEntry, ArchiveProjection, SAFE_FAILURE_MESSAGE, SAFE_REDACTED_METADATA,
    TranscriptExport, _safe_attachment_filename, _safe_closed_metadata, _safe_display_metadata,
    _safe_installation_id, _safe_provider_model_id, metadata_status_matches, safe_finish_reason,
)
from bots5.core.interchange import InterchangeError, canonical_json_bytes, logical_content_digest, parse_jsonl, sha256_hex, strict_json_loads
from bots5.domain.provider import CAPABILITY_KEYS, CapabilityState, validate_capability_value
from bots5.infrastructure.archive_v2 import ArchiveV2Error, ArchiveV2Unsupported, validate_v2


MAX_ENTRIES = 4096
MAX_PATH_BYTES = 240
MAX_PATH_DEPTH = 8
MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_METADATA_ENTRY_BYTES = 32 * 1024 * 1024
MAX_PAYLOAD_ENTRY_BYTES = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
_CAPTURE_CHUNK_BYTES = 1024 * 1024
_REQUIRED = frozenset({
    "manifest.json", "domain/chat.json", "domain/messages.jsonl", "domain/attempts.jsonl",
    "domain/context-plans.jsonl", "domain/attachments.jsonl", "domain/message-attachments.jsonl",
    "domain/attempt-attachments.jsonl", "domain/chat-configuration.json", "domain/provenance.json", "COMPLETED",
})
_JSON = frozenset({"manifest.json", "domain/chat.json", "domain/chat-configuration.json", "domain/provenance.json"})
_JSONL = frozenset({
    "domain/messages.jsonl", "domain/attempts.jsonl", "domain/context-plans.jsonl",
    "domain/attachments.jsonl", "domain/message-attachments.jsonl", "domain/attempt-attachments.jsonl",
})
_SECRET_SHAPED = frozenset({
    "apikey", "apikeyvalue", "apitoken", "accesstoken", "authorization",
    "clientsecret", "password", "refreshtoken", "secret", "secretvalue", "token",
    "credentialreference", "credentialsource", "credentialstatus", "apikeyenv",
    "baseurl", "endpoint", "requestid",
})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_FEATURES = frozenset({"chat-lineage", "generation-outcomes", "request-time-provenance", "context-plans", "attachments"})
_CAPABILITY_STATES = frozenset(item.value for item in CapabilityState)
_CAPABILITY_SOURCES = frozenset({"manual", "confirmed_endpoint", "provider_metadata", "trusted_registry", "heuristic", "unknown"})
_SETTINGS_PROVENANCE = frozenset({"application", "model", "chat_model"})
_OMITTED_SETTINGS = frozenset({"emitted", "unset", "BOTS-owned deadline"})
_ENTRY_MEDIA_TYPES = {
    "domain/chat.json": "application/json",
    "domain/messages.jsonl": "application/x-ndjson",
    "domain/attempts.jsonl": "application/x-ndjson",
    "domain/context-plans.jsonl": "application/x-ndjson",
    "domain/attachments.jsonl": "application/x-ndjson",
    "domain/message-attachments.jsonl": "application/x-ndjson",
    "domain/attempt-attachments.jsonl": "application/x-ndjson",
    "domain/chat-configuration.json": "application/json",
    "domain/provenance.json": "application/json",
    "COMPLETED": "application/octet-stream",
}
_LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")
_EOCD = struct.Struct("<IHHHHIIH")
_CENTRAL_DIRECTORY = struct.Struct("<IHHHHHHIIIHHHHHII")


def _fail(message: str) -> None:
    raise ArchivePackageError(message)


def _exact_mapping(value: object, fields: set[str], label: str) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != fields:
        _fail(f"{label} is not a closed schema")
    return value


def _text(value: object, label: str, *, allow_empty: bool = False, maximum: int = 4096) -> str:
    if type(value) is not str or (not allow_empty and not value) or len(value) > maximum or "\x00" in value:
        _fail(f"{label} is malformed")
    return value


def _available_metadata(value: object, projector, label: str) -> None:
    if projector(value) != (value, "available"):
        _fail(f"{label} is not safe exporter metadata")


def _projected_metadata(value: object, projector, label: str) -> None:
    if projector(value)[0] != value:
        _fail(f"{label} is not a closed exporter metadata projection")


def _status_metadata(value: object, status: object, projector, label: str) -> None:
    if not metadata_status_matches(value, status, projector):
        _fail(f"{label} status/value correspondence is invalid")


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail(f"{label} is malformed")
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        _fail(f"{label} is malformed")
    return value


def _member(value: object, allowed: frozenset[str] | set[str], label: str) -> str:
    if type(value) is not str or value not in allowed:
        _fail(f"{label} is invalid")
    return value


def _none_or_member(value: object, allowed: frozenset[str] | set[str], label: str) -> str | None:
    if value is None:
        return None
    return _member(value, allowed, label)


def _valid_entry_media_type(path: str) -> str:
    if path in _ENTRY_MEDIA_TYPES:
        return _ENTRY_MEDIA_TYPES[path]
    prefix = "payloads/sha256/"
    if path.startswith(prefix):
        _digest(path.removeprefix(prefix), "payload entry path digest")
        return "application/octet-stream"
    _fail("archive entry is outside the Archive v1 namespace")


def _validate_capability(value: object) -> Mapping[str, object]:
    capability = _exact_mapping(value, {"key", "key_status", "state", "source", "source_revision", "value"}, "request capability")
    key = _member(capability["key"], CAPABILITY_KEYS, "request capability key")
    _status_metadata(
        capability["key"], capability["key_status"],
        lambda item: _safe_closed_metadata(item, CAPABILITY_KEYS),
        "request capability key",
    )
    if capability["key_status"] != "available":
        _fail("request capability key must not be redacted")
    state = _member(capability["state"], _CAPABILITY_STATES, "request capability state")
    source = _member(capability["source"], _CAPABILITY_SOURCES, "request capability source")
    revision = capability["source_revision"]
    if revision is not None:
        _integer(revision, "request capability revision")
    if source in {"confirmed_endpoint", "provider_metadata"} and revision is None:
        _fail("request capability revision is required")
    try:
        validate_capability_value(key, CapabilityState(state), capability["value"])
    except (TypeError, ValueError) as exc:
        raise ArchivePackageError("request capability value is invalid") from exc
    return capability


def _digest(value: object, label: str) -> str:
    value = _text(value, label, maximum=64)
    if _DIGEST.fullmatch(value) is None:
        _fail(f"{label} is not lowercase SHA-256")
    return value


def _timestamp(value: object, label: str, *, milliseconds: bool = False) -> str:
    value = _text(value, label, maximum=32)
    if not value.endswith("Z"):
        _fail(f"{label} is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _fail(f"{label} is not canonical UTC")
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        _fail(f"{label} is not canonical UTC")
    precision = "milliseconds" if milliseconds else "microseconds"
    canonical = parsed.astimezone(UTC).isoformat(timespec=precision).replace("+00:00", "Z")
    if canonical != value:
        _fail(f"{label} is not canonical UTC")
    return value


def _decimal(value: object, label: str) -> str:
    value = _text(value, label, maximum=128)
    if _DECIMAL.fullmatch(value) is None:
        _fail(f"{label} is not a canonical decimal")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        _fail(f"{label} is not a canonical decimal")
    if not parsed.is_finite():
        _fail(f"{label} is not a canonical decimal")
    return value


class ArchivePackageError(ValueError):
    pass


class ArchiveUnsupportedError(ArchivePackageError):
    """The package declares a future semantic this build cannot interpret."""


@dataclass(frozen=True, slots=True)
class ArchiveValidationResult:
    archive_id: str
    logical_content_digest: str
    entry_count: int
    total_uncompressed_size: int


def _path(name: str) -> str:
    if type(name) is not str or not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise ArchivePackageError("archive entry path is unsafe")
    pieces = name.split("/")
    if any(not part or part in {".", ".."} for part in pieces):
        raise ArchivePackageError("archive entry path is unsafe")
    if len(pieces) > MAX_PATH_DEPTH or len(name.encode("utf-8")) > MAX_PATH_BYTES:
        raise ArchivePackageError("archive entry path exceeds bounded limits")
    return name


def _collision_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _zip_info(name: str) -> zipfile.ZipInfo:
    result = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    # Archive v1's reader rejects pathological compression ratios.  Using the
    # supported stored representation keeps ordinary repetitive domain text or
    # payloads inside that same language rather than self-producing a package
    # the reader must reject.
    result.compress_type = zipfile.ZIP_STORED
    result.external_attr = (stat.S_IFREG | 0o600) << 16
    result.create_system = 3
    return result


def _inventory(entries: tuple[ArchiveLogicalEntry, ...]) -> list[dict[str, object]]:
    return [
        {"path": item.path, "media_type": item.media_type, "required": item.required,
         "uncompressed_size": len(item.content), "sha256": sha256_hex(item.content)}
        for item in sorted(entries, key=lambda item: item.path)
    ]


def archive_bytes(projection: ArchiveProjection) -> bytes:
    if projection.manifest_base.get("archive_version") == 2:
        from .archive_v2 import archive_v2_bytes
        return archive_v2_bytes(
            projection.manifest_base,
            {entry.path: entry.content for entry in projection.entries},
        )
    import io
    stream = io.BytesIO()
    _write(projection, stream)
    result = stream.getvalue()
    # The writer has to close the same semantic language that the reader
    # accepts.  The reader still has independently-constructed mutant tests;
    # this is only the writer-side refusal of an invalid projection.
    validate_archive(io.BytesIO(result))
    return result


def _write(projection: ArchiveProjection, stream: BinaryIO) -> None:
    entries = (*projection.entries, ArchiveLogicalEntry("COMPLETED", "application/octet-stream", True, b""))
    names = [item.path for item in entries]
    if len(names) != len(set(names)) or any(_path(name) != name for name in names):
        raise ArchivePackageError("writer received unsafe or duplicate logical entries")
    inventory = _inventory(entries)
    digest = logical_content_digest((str(item["path"]), int(item["uncompressed_size"]), str(item["sha256"])) for item in inventory)
    manifest = dict(projection.manifest_base)
    manifest.update({"entry_inventory": inventory, "logical_content_digest": digest})
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True, strict_timestamps=True) as package:
        package.writestr(_zip_info("manifest.json"), canonical_json_bytes(manifest))
        for entry in sorted(entries, key=lambda item: (item.path == "COMPLETED", item.path)):
            package.writestr(_zip_info(entry.path), entry.content)


def _destination_parent(destination: Path, suffix: str) -> tuple[int, str]:
    """Open every ancestor without ever resolving a mutable pathname.

    The returned parent descriptor remains held through publication.  This is
    the identity used for temporary creation, validation, link publication,
    cleanup, and the final directory durability fence.
    """
    raw = os.fspath(destination)
    if type(raw) is not str or not raw or "\x00" in raw or "\\" in raw:
        _fail("output destination is unsafe")
    absolute = raw.startswith(os.sep)
    pieces = raw.split(os.sep)
    if absolute:
        pieces = pieces[1:]
    if not pieces or any(not part or part in {".", ".."} for part in pieces):
        _fail("output destination is unsafe")
    final = pieces[-1]
    if not final.endswith(suffix) or final in {".", ".."}:
        _fail(f"output must use {suffix}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(os.sep if absolute else ".", flags)
        for component in pieces[:-1]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as exc:
        try:
            os.close(descriptor)
        except (UnboundLocalError, OSError):
            pass
        raise ArchivePackageError("output ancestor is unavailable or unsafe") from exc
    return descriptor, final


def _partial_open(parent_fd: int, final: str) -> tuple[int, str]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for _ in range(32):
        partial = f".{final}.{secrets.token_hex(16)}.partial"
        try:
            return os.open(partial, flags, 0o600, dir_fd=parent_fd), partial
        except FileExistsError:
            continue
    raise ArchivePackageError("could not reserve output partial name")


def _publish(parent_fd: int, partial: str, final: str) -> None:
    try:
        os.link(partial, final, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
    except FileExistsError:
        raise FileExistsError(final) from None
    # Once link succeeds the final name may exist even if the subsequent
    # directory fsync fails.  Keep that observable state; never use an
    # unresolved pathname rollback that could target a replaced ancestor.
    os.unlink(partial, dir_fd=parent_fd)
    os.fsync(parent_fd)


def write_archive(projection: ArchiveProjection, destination: Path) -> ArchiveValidationResult:
    parent_fd, final = _destination_parent(destination, ".botsarchive")
    partial: str | None = None
    fd: int | None = None
    try:
        fd, partial = _partial_open(parent_fd, final)
        with os.fdopen(os.dup(fd), "wb") as stream:
            _write(projection, stream)
            stream.flush()
        os.fsync(fd)
        with os.fdopen(os.dup(fd), "rb") as stream:
            result = validate_archive(stream)
        _publish(parent_fd, partial, final)
        partial = None
        return result
    except BaseException:
        if partial is not None:
            try:
                os.unlink(partial, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def write_transcript(projection: TranscriptExport, destination: Path) -> None:
    """Safely publish a pre-rendered Transcript v0.1 without overwrite."""
    raw = projection.content
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw or not raw.endswith(b"\n"):
        _fail("Transcript v0.1 bytes are not canonical UTF-8/LF")
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ArchivePackageError("Transcript v0.1 is not UTF-8") from exc
    parent_fd, final = _destination_parent(destination, ".md")
    partial: str | None = None
    fd: int | None = None
    try:
        fd, partial = _partial_open(parent_fd, final)
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(raw)
            stream.flush()
        os.fsync(fd)
        _publish(parent_fd, partial, final)
        partial = None
    except BaseException:
        if partial is not None:
            try:
                os.unlink(partial, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def _regular_file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    if not stat.S_ISREG(info.st_mode) or info.st_size < 0:
        _fail("archive source is unavailable")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def _captured_archive_input(source: Path | BinaryIO) -> Iterator[BinaryIO]:
    """Create the one private byte snapshot used by the reader.

    The preliminary central-directory admission keeps the existing bounded
    member-map guarantee before any full disk copy.  Archive v1 has no global
    physical-container byte cap, so the copy is bounded to the exact length
    observed from this source; the temporary file bounds memory to one chunk.
    The same private bytes are preflighted again and then passed to ZipFile.
    """
    stream: BinaryIO | None = None
    close_stream = False
    original_position: int | None = None
    path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | None = None
    source_identity: tuple[int, int, int, int, int] | None = None
    try:
        if isinstance(source, (str, os.PathLike)):
            path = source
            stream = open(source, "rb")
            close_stream = True
            source_identity = _regular_file_identity(os.fstat(stream.fileno()))
        elif hasattr(source, "seek") and hasattr(source, "tell") and hasattr(source, "read"):
            stream = source
            original_position = stream.tell()
            try:
                source_identity = _regular_file_identity(os.fstat(stream.fileno()))
            except (AttributeError, OSError):
                pass
        else:
            _fail("archive source is not seekable")

        # This screen has bounded allocation: it reads only the bounded EOCD
        # tail and bounded central directory, and never invokes ZipFile on
        # external bytes.
        _preflight_central_directory(stream)

        stream.seek(0, os.SEEK_END)
        expected_size = stream.tell()
        if type(expected_size) is not int or expected_size < 0:
            _fail("archive source size is invalid")
        stream.seek(0)
        with tempfile.TemporaryFile(mode="w+b") as snapshot:
            remaining = expected_size
            while remaining:
                requested = min(_CAPTURE_CHUNK_BYTES, remaining)
                block = stream.read(requested)
                if not block:
                    _fail("archive input capture is incomplete")
                if type(block) is not bytes or len(block) > requested:
                    _fail("archive input capture overflow")
                written = snapshot.write(block)
                if written != len(block):
                    _fail("archive input capture is incomplete")
                remaining -= len(block)
            if stream.read(1):
                _fail("archive input capture overflow")
            if source_identity is not None:
                current_identity = _regular_file_identity(os.fstat(stream.fileno()))
                if current_identity != source_identity:
                    _fail("archive source changed during capture")
                if path is not None and _regular_file_identity(os.stat(path)) != source_identity:
                    _fail("archive source changed during capture")
            else:
                stream.seek(0, os.SEEK_END)
                if stream.tell() != expected_size:
                    _fail("archive source changed during capture")
            snapshot.flush()
            snapshot.seek(0)
            if close_stream:
                stream.close()
                stream = None
                close_stream = False
            yield snapshot
    except ArchivePackageError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise ArchivePackageError("archive input capture failed") from exc
    finally:
        if stream is not None and not close_stream and original_position is not None:
            try:
                stream.seek(original_position)
            except (OSError, ValueError):
                pass
        if close_stream and stream is not None:
            stream.close()


def validate_archive(source: Path | BinaryIO) -> ArchiveValidationResult:
    with _captured_archive_input(source) as snapshot:
        raw_names = _preflight_central_directory(snapshot)
        try:
            package = zipfile.ZipFile(snapshot, "r")
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise ArchivePackageError("invalid archive container") from exc
        with package:
            infos = package.infolist()
            if not 1 <= len(infos) <= MAX_ENTRIES:
                raise ArchivePackageError("archive entry count exceeds limit")
            if len(raw_names) != len(infos) or any(name != info.orig_filename for name, info in zip(raw_names, infos, strict=True)):
                raise ArchivePackageError("archive central directory names are inconsistent")
            names: set[str] = set()
            collision: set[str] = set()
            total = 0
            contents: dict[str, bytes] = {}
            for info in infos:
                name = _path(info.filename)
                if name in names or _collision_key(name) in collision:
                    raise ArchivePackageError("archive has duplicate or colliding entry names")
                names.add(name); collision.add(_collision_key(name))
                mode = info.external_attr >> 16
                if stat.S_IFMT(mode) != stat.S_IFREG or info.is_dir():
                    raise ArchivePackageError("archive contains a non-regular entry")
                if info.flag_bits & ~0x800:
                    raise ArchivePackageError("encrypted archives are unsupported")
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise ArchivePackageError("archive compression method is unsupported")
                _validate_local_header(package, info)
                maximum = MAX_PAYLOAD_ENTRY_BYTES if name.startswith("payloads/sha256/") else MAX_METADATA_ENTRY_BYTES
                if info.file_size < 0 or info.file_size > maximum:
                    raise ArchivePackageError("archive entry exceeds bounded size")
                if info.compress_size == 0 and info.file_size:
                    raise ArchivePackageError("archive compression ratio is unsafe")
                if info.compress_size and info.file_size > info.compress_size * MAX_COMPRESSION_RATIO:
                    raise ArchivePackageError("archive compression ratio exceeds limit")
                total += info.file_size
                if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise ArchivePackageError("archive total size exceeds limit")
                with package.open(info, "r") as member:
                    chunks: list[bytes] = []
                    remaining = info.file_size
                    digest = hashlib.sha256()
                    while remaining:
                        block = member.read(min(65536, remaining))
                        if not block:
                            raise ArchivePackageError("archive member is truncated")
                        remaining -= len(block); digest.update(block); chunks.append(block)
                    if member.read(1):
                        raise ArchivePackageError("archive member size is inconsistent")
                contents[name] = b"".join(chunks)
            if not _REQUIRED <= names:
                raise ArchivePackageError("archive lacks a required entry")
            if infos[-1].filename != "COMPLETED" or contents.get("COMPLETED") != b"":
                raise ArchivePackageError("COMPLETED must be the final zero-length entry")
            manifest = _json_object(contents["manifest.json"], "manifest")
            _reject_secret_shaped_fields(manifest)
            version = manifest.get("archive_version")
            if type(version) is int and version not in {1, 2}:
                raise ArchiveUnsupportedError("Archive version is unsupported")
            if version == 1:
                unknown_members = names - {"manifest.json", "COMPLETED"} - _REQUIRED
                if any(not name.startswith("payloads/sha256/") for name in unknown_members):
                    raise ArchiveUnsupportedError("Archive member is unsupported")
            # The frozen v1 branch below remains exact.  V2 gets a separate
            # closed validator so no v1 optionality or graph assumption is
            # accidentally reinterpreted as an import-provenance language.
            if manifest.get("archive_version") == 2:
                try:
                    result = validate_v2(manifest, names, contents, total_size=total)
                except ArchiveV2Unsupported as exc:
                    raise ArchiveUnsupportedError("Archive v2 semantic is unsupported") from exc
                except ArchiveV2Error as exc:
                    raise ArchivePackageError("Archive v2 is invalid") from exc
                return ArchiveValidationResult(
                    result.archive_id, result.logical_content_digest,
                    result.entry_count, result.total_uncompressed_size,
                )
            _validate_manifest(manifest, names, contents)
            for name in _JSON:
                _reject_secret_shaped_fields(_json_object(contents[name], name))
            for name in _JSONL:
                _reject_secret_shaped_fields(parse_jsonl(contents[name]))
            _validate_graph(contents, manifest)
            return ArchiveValidationResult(str(manifest["archive_id"]), str(manifest["logical_content_digest"]), len(infos), total)


def _json_object(raw: bytes, name: str) -> dict[str, object]:
    try:
        value = strict_json_loads(raw)
    except InterchangeError as exc:
        raise ArchivePackageError(f"{name} is malformed JSON") from exc
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise ArchivePackageError(f"{name} must be a JSON object")
    return value


def _reject_secret_shaped_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            compact = "".join(character for character in key.casefold() if character.isalnum())
            if compact in _SECRET_SHAPED:
                raise ArchivePackageError("archive contains a secret-shaped exporter field")
            _reject_secret_shaped_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_secret_shaped_fields(nested)


def _preflight_central_directory(source: Path | BinaryIO) -> tuple[str, ...]:
    """Bound central-directory claims before ZipFile can allocate its member map."""
    stream: BinaryIO | None = None
    close = False
    try:
        if isinstance(source, (str, os.PathLike)):
            stream = open(source, "rb")
            close = True
        elif hasattr(source, "seek") and hasattr(source, "tell") and hasattr(source, "read"):
            stream = source
        else:
            return ()
        position = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if size < _EOCD.size:
            _fail("archive central directory is truncated")
        tail_size = min(size, 65557 + _EOCD.size)
        stream.seek(size - tail_size)
        tail = stream.read(tail_size)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < _EOCD.size:
            _fail("archive central directory is malformed")
        _, disk, directory_disk, disk_entries, entries, directory_size, directory_offset, comment = _EOCD.unpack_from(tail, offset)
        if disk or directory_disk or disk_entries != entries or comment != len(tail) - offset - _EOCD.size:
            _fail("multi-volume archive is unsupported")
        # Zip64 count/offset sentinels are rejected rather than passed to the
        # standard-library central-directory allocator.  They are necessarily
        # above Archive v1's bounded member limits.
        if entries == 0xFFFF or directory_size == 0xFFFFFFFF or directory_offset == 0xFFFFFFFF:
            _fail("Zip64 central directory exceeds Archive v1 limits")
        if entries > MAX_ENTRIES or directory_size > MAX_ENTRIES * 512 or directory_offset + directory_size > size:
            _fail("archive central directory exceeds bounded limits")
        stream.seek(directory_offset)
        directory = stream.read(directory_size)
        if len(directory) != directory_size:
            _fail("archive central directory is truncated")
        names: list[str] = []
        cursor = 0
        for _ in range(entries):
            if cursor + _CENTRAL_DIRECTORY.size > len(directory):
                _fail("archive central directory is malformed")
            fields = _CENTRAL_DIRECTORY.unpack_from(directory, cursor)
            signature, _, _, flags, _, _, _, _, _, _, name_size, extra_size, comment_size, _, _, _, _ = fields
            if signature != 0x02014B50 or flags & ~0x800:
                _fail("archive central directory is malformed")
            cursor += _CENTRAL_DIRECTORY.size
            end = cursor + name_size + extra_size + comment_size
            if end > len(directory):
                _fail("archive central directory is truncated")
            raw_name = directory[cursor:cursor + name_size]
            if b"\x00" in raw_name:
                _fail("archive entry path is unsafe")
            try:
                name = raw_name.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                _fail("archive entry path is not UTF-8")
            _path(name)
            names.append(name)
            cursor = end
        if cursor != len(directory):
            _fail("archive central directory has trailing data")
        return tuple(names)
    except (OSError, ValueError, struct.error) as exc:
        raise ArchivePackageError("archive central directory is malformed") from exc
    finally:
        if stream is not None and not close:
            try:
                stream.seek(position)
            except (OSError, ValueError, UnboundLocalError):
                pass
        if close and stream is not None:
            stream.close()


def _validate_local_header(package: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    stream = package.fp
    if stream is None:
        _fail("archive stream is unavailable")
    position = stream.tell()
    try:
        stream.seek(info.header_offset)
        header = stream.read(_LOCAL_HEADER.size)
        if len(header) != _LOCAL_HEADER.size:
            _fail("archive local header is truncated")
        signature, _, flags, method, _, _, crc, compressed, uncompressed, name_len, extra_len = _LOCAL_HEADER.unpack(header)
        if signature != 0x04034B50 or flags != info.flag_bits or method != info.compress_type:
            _fail("archive local header contradicts central directory")
        if flags & 0x08 or (crc, compressed, uncompressed) != (info.CRC, info.compress_size, info.file_size):
            _fail("archive local metadata is unsupported or inconsistent")
        name = stream.read(name_len)
        if name != info.orig_filename.encode("utf-8"):
            _fail("archive local filename contradicts central directory")
        if len(stream.read(extra_len)) != extra_len:
            _fail("archive local header is truncated")
    finally:
        stream.seek(position)


def _validate_manifest(manifest: Mapping[str, object], names: set[str], contents: Mapping[str, bytes]) -> None:
    fields = {"format", "archive_version", "archive_id", "created_at", "source_application_version", "source_db_migration_revision", "source_chat", "attachment_policy", "self_contained", "features", "entry_inventory", "external_resources", "logical_content_digest", "secret_exclusion"}
    _exact_mapping(manifest, fields, "manifest")
    if manifest["format"] != "org.necromilias.bots5.chat-archive" or type(manifest["archive_version"]) is not int or manifest["archive_version"] != 1:
        _fail("archive format/version is unsupported")
    _text(manifest["archive_id"], "manifest archive ID")
    _timestamp(manifest["created_at"], "manifest creation timestamp")
    _text(manifest["source_application_version"], "source application version")
    _text(manifest["source_db_migration_revision"], "source database migration revision")
    source_chat = _exact_mapping(manifest["source_chat"], {"source_id", "title"}, "manifest source chat")
    _text(source_chat["source_id"], "manifest source chat ID")
    _text(source_chat["title"], "manifest source chat title", allow_empty=True)
    features = manifest["features"]
    if type(features) is not list or any(type(item) is not str for item in features) or set(features) != _FEATURES or len(features) != len(_FEATURES):
        _fail("archive feature set is unsupported")
    inventory = manifest["entry_inventory"]
    if type(inventory) is not list or not inventory:
        _fail("manifest inventory is malformed")
    declared: dict[str, Mapping[str, object]] = {}
    collision: set[str] = set()
    for item in inventory:
        item = _exact_mapping(item, {"path", "media_type", "required", "uncompressed_size", "sha256"}, "manifest inventory row")
        path = _text(item["path"], "manifest inventory path", maximum=MAX_PATH_BYTES)
        if path == "manifest.json" or _path(path) != path or path in declared or _collision_key(path) in collision:
            _fail("manifest inventory path is malformed")
        collision.add(_collision_key(path))
        media_type = _text(item["media_type"], "manifest media type")
        if media_type != _valid_entry_media_type(path):
            _fail("manifest inventory media type is invalid")
        if _boolean(item["required"], "manifest inventory required") is not True:
            _fail("Archive v1 entries must be required")
        _integer(item["uncompressed_size"], "manifest inventory size")
        _digest(item["sha256"], "manifest inventory digest")
        declared[path] = item
    if set(declared) != names - {"manifest.json"}:
        _fail("archive has undeclared or missing entries")
    tuples = []
    for path, item in declared.items():
        raw = contents[path]
        if len(raw) != item["uncompressed_size"] or sha256_hex(raw) != item["sha256"]:
            _fail("archive entry integrity check failed")
        tuples.append((path, item["uncompressed_size"], item["sha256"]))
    if _digest(manifest["logical_content_digest"], "logical content digest") != logical_content_digest(tuples):
        _fail("archive logical content digest is invalid")
    policy = manifest["attachment_policy"]
    if _member(policy, {"embedded", "external-reference"}, "manifest attachment policy") not in {"embedded", "external-reference"} or _boolean(manifest["self_contained"], "manifest self-contained") != (policy == "embedded"):
        _fail("archive attachment policy is incoherent")
    _text(manifest["secret_exclusion"], "secret exclusion declaration")
    external = manifest["external_resources"]
    if type(external) is not list:
        _fail("external resource declarations are malformed")
    resources: set[str] = set()
    for item in external:
        item = _exact_mapping(item, {"digest", "size", "logical_resource_id", "required"}, "external resource declaration")
        digest = _digest(item["digest"], "external resource digest")
        if digest in resources or _integer(item["size"], "external resource size") < 0 or item["logical_resource_id"] != f"sha256:{digest}" or _boolean(item["required"], "external resource required") is not True:
            _fail("external resource declaration is malformed")
        resources.add(digest)
    if policy == "embedded" and external:
        _fail("embedded archive declares external resources")
    if policy == "external-reference" and any(name.startswith("payloads/") for name in names):
        _fail("external archive embeds payloads")


def _rows(contents: Mapping[str, bytes], path: str) -> tuple[Mapping[str, object], ...]:
    try:
        raw = parse_jsonl(contents[path])
    except InterchangeError as exc:
        raise ArchivePackageError(f"{path} is malformed JSONL") from exc
    if any(type(item) is not dict for item in raw):
        _fail(f"{path} must contain JSON objects")
    return raw  # type: ignore[return-value]


def _validate_chat(value: object) -> Mapping[str, object]:
    chat = _exact_mapping(value, {"source_id", "title", "created_at", "updated_at", "archived_at", "head_message_id", "revision"}, "chat")
    _text(chat["source_id"], "chat ID")
    _text(chat["title"], "chat title", allow_empty=True)
    _timestamp(chat["created_at"], "chat creation timestamp")
    _timestamp(chat["updated_at"], "chat update timestamp")
    if chat["archived_at"] is not None:
        _timestamp(chat["archived_at"], "chat archive timestamp")
    if chat["head_message_id"] is not None:
        _text(chat["head_message_id"], "chat head ID")
    _integer(chat["revision"], "chat revision")
    return chat


def _validate_message(value: object, chat_id: str) -> Mapping[str, object]:
    row = _exact_mapping(value, {"source_id", "chat_id", "role", "state", "content", "sequence", "created_at", "parent_id", "lineage_id", "revision", "supersedes_id"}, "message")
    _text(row["source_id"], "message ID")
    if row["chat_id"] != chat_id:
        _fail("message belongs to a different chat")
    role = _member(row["role"], {"user", "assistant"}, "message role")
    allowed = {"user": {"sent"}, "assistant": {"complete", "incomplete", "failed", "truncated", "aborted"}}
    _member(row["state"], allowed[role], "message lifecycle")
    _text(row["content"], "message content", allow_empty=True, maximum=MAX_METADATA_ENTRY_BYTES)
    _integer(row["sequence"], "message sequence", minimum=1)
    _timestamp(row["created_at"], "message timestamp")
    for key in ("parent_id", "supersedes_id"):
        if row[key] is not None:
            _text(row[key], f"message {key}")
    _text(row["lineage_id"], "message lineage ID")
    _integer(row["revision"], "message revision", minimum=1)
    return row


def _validate_provenance(
    value: object,
    *,
    settings_provenance_values: frozenset[str] = _SETTINGS_PROVENANCE,
) -> Mapping[str, object]:
    fields = {"status", "snapshot_version", "attribution"}
    if type(value) is not dict or type(value.get("status")) is not str:
        _fail("request-time provenance is malformed")
    status = value["status"]
    if status in {"corrupt", "unsupported"}:
        return _exact_mapping(value, {"status"}, "request-time provenance")
    if status == "available":
        version = value.get("snapshot_version")
        if type(version) is not int or version not in {2, 3}:
            _fail("request-time provenance version is invalid")
        expected = fields | {"settings", "settings_provenance", "capabilities", "manual_overrides", "omitted_settings"}
        if version == 3:
            expected |= {"settings_revisions", "context"}
        row = _exact_mapping(value, expected, "available request-time provenance")
    elif status == "legacy-limited":
        row = _exact_mapping(value, fields, "legacy request-time provenance")
    else:
        _fail("request-time provenance status is invalid")
    status = row["status"]
    if status == "available":
        version = row["snapshot_version"]
    elif status != "legacy-limited" or row["snapshot_version"] != "legacy":
        _fail("request-time provenance status is invalid")
    attribution = _exact_mapping(row["attribution"], {"backend_id", "provider_id", "provider_id_status", "model", "model_status"} | ({"provider_profile", "connection_revision", "catalogue_revision"} if status == "available" else set()), "request attribution")
    for key in ("backend_id", "provider_id", "provider_id_status", "model", "model_status"):
        _text(attribution[key], f"request attribution {key}")
    _projected_metadata(
        attribution["backend_id"],
        lambda item: _safe_closed_metadata(item, frozenset({"fake", "openai_compatible_http"})),
        "request attribution backend",
    )
    _status_metadata(
        attribution["provider_id"], attribution["provider_id_status"],
        lambda item: _safe_closed_metadata(item, frozenset({"fake", "generic", "local_openai", "openrouter"})),
        "request provider attribution",
    )
    _status_metadata(attribution["model"], attribution["model_status"], _safe_provider_model_id, "request model attribution")
    if status == "available":
        _available_metadata(
            attribution["provider_profile"],
            lambda item: _safe_closed_metadata(item, frozenset({"generic", "openrouter"})),
            "request attribution profile",
        )
        _integer(attribution["connection_revision"], "request connection revision")
        _integer(attribution["catalogue_revision"], "request catalogue revision")
        settings = _exact_mapping(row["settings"], {"temperature", "max_output_tokens", "reasoning_effort", "timeout_seconds"}, "request settings")
        if type(settings["temperature"]) not in {int, float} or type(settings["temperature"]) is bool or not math.isfinite(settings["temperature"]) or not 0 <= settings["temperature"] <= 2:
            _fail("request temperature is invalid")
        _integer(settings["max_output_tokens"], "request max output", minimum=1)
        if _none_or_member(settings["reasoning_effort"], {"none"}, "request reasoning effort") not in {None, "none"} or (settings["timeout_seconds"] is not None and (type(settings["timeout_seconds"]) not in {int, float} or type(settings["timeout_seconds"]) is bool or not math.isfinite(settings["timeout_seconds"]) or settings["timeout_seconds"] <= 0)):
            _fail("request settings are invalid")
        provenance = _exact_mapping(row["settings_provenance"], {"temperature", "max_output_tokens", "reasoning_effort", "timeout_seconds"}, "request settings provenance")
        for item in provenance.values():
            _member(item, settings_provenance_values, "request settings provenance")
        if type(row["capabilities"]) is not list or type(row["manual_overrides"]) is not dict or type(row["omitted_settings"]) is not dict:
            _fail("request-time provenance values are malformed")
        capabilities: dict[str, Mapping[str, object]] = {}
        for capability_value in row["capabilities"]:
            capability = _validate_capability(capability_value)
            if capability["key"] in capabilities:
                _fail("request capability identities are duplicated")
            capabilities[capability["key"]] = capability
        if set(capabilities) != CAPABILITY_KEYS:
            _fail("request capability facts are incomplete")
        for key, override in row["manual_overrides"].items():
            _member(key, CAPABILITY_KEYS, "request manual override key")
            override = _exact_mapping(override, {"state", "value", "revision"}, "request manual override")
            state = _member(override["state"], _CAPABILITY_STATES, "request manual override state")
            revision = _integer(override["revision"], "request manual override revision", minimum=1)
            try:
                validate_capability_value(key, CapabilityState(state), override["value"])
            except (TypeError, ValueError) as exc:
                raise ArchivePackageError("request manual override value is invalid") from exc
            fact = capabilities[key]
            if fact["source"] != "manual" or fact["state"] != state or fact["value"] != override["value"] or fact["source_revision"] != revision:
                _fail("request manual override contradicts capability fact")
        if any(fact["source"] == "manual" and key not in row["manual_overrides"] for key, fact in capabilities.items()):
            _fail("manual capability fact lacks its override")
        omitted = _exact_mapping(row["omitted_settings"], {"temperature", "max_output_tokens", "reasoning_effort", "timeout_seconds"}, "request omitted settings")
        for item in omitted.values():
            _member(item, _OMITTED_SETTINGS, "request omitted settings")
        if row["snapshot_version"] == 3:
            revisions = _exact_mapping(row["settings_revisions"], {"application", "model", "chat"}, "request settings revisions")
            _integer(revisions["application"], "request application settings revision", minimum=1)
            for key in ("model", "chat"):
                if revisions[key] is not None:
                    _integer(revisions[key], f"request {key} settings revision", minimum=1)
            _validate_safe_context(row["context"])
    return row


def _validate_safe_context(value: object) -> Mapping[str, object]:
    fields = {"version", "canonical_digest", "wire_representation_sha256", "included_sources", "excluded_sources", "sources", "canonical_projection", "canonical_projection_digest", "canonical_projection_status", "budget", "input_counts", "parent_id", "lineage_id", "context_capability"}
    context = _exact_mapping(value, fields, "safe context")
    if context["version"] != 3 or type(context["version"]) is not int:
        _fail("safe context version is invalid")
    _digest(context["canonical_digest"], "safe context canonical digest")
    _digest(context["wire_representation_sha256"], "safe context wire digest")
    _digest(context["canonical_projection_digest"], "safe context projection digest")
    _member(context["canonical_projection_status"], {"matches-request-time", "sanitized"}, "safe context projection status")
    projection = context["canonical_projection"]
    if type(projection) is not dict or sha256_hex(canonical_json_bytes(projection)[:-1]) != context["canonical_projection_digest"]:
        _fail("safe context projection digest is invalid")
    expected_projection = {"version", "sources", "included_sources", "excluded_sources", "envelope", "wire_sha256"}
    _exact_mapping(projection, expected_projection, "safe context canonical projection")
    if projection["version"] != 3 or projection["wire_sha256"] != context["wire_representation_sha256"] or projection["included_sources"] != context["included_sources"] or projection["excluded_sources"] != context["excluded_sources"]:
        _fail("safe context projection contradicts context")
    _exact_mapping(projection["envelope"], {"version", "untrusted_user_context"}, "safe context envelope")
    if projection["envelope"] != {"version": 3, "untrusted_user_context": True}:
        _fail("safe context envelope is invalid")
    if type(context["sources"]) is not list or type(context["included_sources"]) is not list or type(context["excluded_sources"]) is not list or projection["sources"] != [
        {"source_id": item.get("source_id"), "kind": item.get("kind"), "role": item.get("role"), "content": item.get("content"), "state": item.get("state"), "eligible": item.get("eligible"), "selected": item.get("selected"), "reason": item.get("selection_reason"), "representation_id": item.get("representation_id"), "representation_digest": item.get("representation_digest")} for item in context["sources"] if type(item) is dict
    ]:
        _fail("safe context sources are malformed")
    source_ids: set[str] = set()
    selected: list[str] = []
    excluded: list[str] = []
    for source in context["sources"]:
        source = _exact_mapping(source, {"source_id", "source_id_status", "kind", "role", "content", "state", "state_status", "eligible", "selected", "selection_reason", "representation_id", "representation_digest"}, "safe context source")
        source_id = _text(source["source_id"], "safe context source ID")
        kind = _member(source["kind"], {"history", "bots_instruction", "current_user", "attachment"}, "safe context source kind")
        role = _member(source["role"], {"user", "assistant", "system"}, "safe context source role")
        _status_metadata(source_id, source["source_id_status"], _safe_installation_id, "safe context source ID")
        _status_metadata(
            source["state"], source["state_status"],
            lambda item: _safe_closed_metadata(item, frozenset({
                "sending", "sent", "failed", "streaming", "complete", "incomplete", "truncated", "aborted",
            })),
            "safe context source state",
        )
        if source_id in source_ids or type(source["content"]) is not str or type(source["eligible"]) is not bool or type(source["selected"]) is not bool:
            _fail("safe context source is malformed")
        source_ids.add(source_id)
        if kind == "history" and role not in {"user", "assistant"}:
            _fail("safe history source role is invalid")
        if kind == "bots_instruction" and role != "system":
            _fail("safe instruction source role is invalid")
        if kind == "current_user" and role != "user":
            _fail("safe current-user source role is invalid")
        if kind == "attachment":
            if source["representation_id"] != source["representation_digest"]:
                _fail("safe attachment representation is invalid")
            _digest(source["representation_id"], "safe attachment representation digest")
        elif source["representation_id"] is not None or source["representation_digest"] is not None:
            _fail("safe non-attachment representation is invalid")
        if source["selected"]:
            if not source["eligible"]:
                _fail("safe selected context source is ineligible")
            _member(source["selection_reason"], {"selected", "unavailable"}, "safe selected context reason")
            selected.append(source_id)
        else:
            if _member(source["selection_reason"], {"excluded_oldest_complete_turn"}, "safe excluded context reason") != "excluded_oldest_complete_turn" or kind != "history":
                _fail("safe excluded context source is invalid")
            excluded.append(source_id)
    if selected != context["included_sources"] or excluded != context["excluded_sources"]:
        _fail("safe context source selection is inconsistent")
    budget = _exact_mapping(context["budget"], {"limit", "semantics", "adapter_id", "adapter_version", "output_reserve", "envelope_overhead", "input_units", "total_units", "headroom"}, "safe context budget")
    for key in ("limit", "output_reserve", "envelope_overhead", "input_units", "total_units", "headroom"):
        _integer(budget[key], f"safe context budget {key}")
    if budget["limit"] <= 0 or budget["total_units"] != budget["input_units"] + budget["envelope_overhead"] + budget["output_reserve"] or budget["headroom"] != budget["limit"] - budget["total_units"] or budget["headroom"] < 0:
        _fail("safe context budget accounting is invalid")
    if type(context["input_counts"]) is not dict or set(context["input_counts"]) != {"history_turns_considered", "history_turns_included", "history_turns_excluded", "mandatory_sources"}:
        _fail("safe context input counts are invalid")
    for value in context["input_counts"].values():
        _integer(value, "safe context input count")
    if context["parent_id"] is not None:
        _text(context["parent_id"], "safe context parent ID")
    if context["lineage_id"] is not None:
        _text(context["lineage_id"], "safe context lineage ID")
    if context["canonical_projection_status"] == "matches-request-time" and context["canonical_digest"] != context["canonical_projection_digest"]:
        _fail("safe context original digest does not match request-time projection")
    return context


def _validate_attempt(
    value: object,
    chat_id: str,
    messages: Mapping[str, Mapping[str, object]],
    *,
    settings_provenance_values: frozenset[str] = _SETTINGS_PROVENANCE,
) -> Mapping[str, object]:
    fields = {"source_id", "chat_id", "user_message_id", "assistant_message_id", "backend_id", "provider_id", "model", "state", "started_at", "ended_at", "finish_reason", "failure", "returned_model", "prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens", "known_cost_usd", "remote_outcome_unknown", "request_time_provenance"}
    row = _exact_mapping(value, fields, "attempt")
    attempt_id = _text(row["source_id"], "attempt ID")
    if row["chat_id"] != chat_id:
        _fail("attempt belongs to a different chat")
    user_id = _text(row["user_message_id"], "attempt user message ID")
    assistant_id = _text(row["assistant_message_id"], "attempt assistant message ID")
    user = messages.get(user_id)
    assistant = messages.get(assistant_id)
    if user is None or assistant is None or user["role"] != "user" or assistant["role"] != "assistant" or assistant["parent_id"] != user_id:
        _fail("attempt user/assistant ownership is invalid")
    for key in ("backend_id",):
        _text(row[key], f"attempt {key}")
    for key in ("provider_id", "model", "finish_reason", "returned_model"):
        if row[key] is not None:
            _text(row[key], f"attempt {key}")
    state = _member(row["state"], {"complete", "incomplete", "failed", "aborted"}, "attempt lifecycle")
    if safe_finish_reason(row["finish_reason"], state) != row["finish_reason"]:
        _fail("attempt finish reason is not a closed outcome")
    _timestamp(row["started_at"], "attempt start timestamp")
    _timestamp(row["ended_at"], "attempt end timestamp")
    if row["ended_at"] < row["started_at"]:
        _fail("attempt end precedes start")
    failure = row["failure"]
    if state == "complete":
        expected_message, expected_finish, expected_failure = "complete", "stop", None
    elif state == "incomplete":
        # A stream which ends without a terminal event has no finish reason
        # and leaves an INCOMPLETE assistant.  A completed non-stop response
        # has its provider finish reason and a TRUNCATED assistant.
        expected_message = "incomplete" if row["finish_reason"] is None else "truncated"
        expected_finish = None
        expected_failure = "generation-incomplete"
        if row["finish_reason"] == "stop":
            _fail("incomplete attempt has stop finish reason")
    elif state == "failed":
        expected_message, expected_finish, expected_failure = "failed", None, "generation-failed"
    else:
        expected_message, expected_finish, expected_failure = "aborted", None, "generation-aborted"
    if assistant["state"] != expected_message:
        _fail("attempt and assistant lifecycles contradict")
    if state == "complete" and row["finish_reason"] != expected_finish:
        _fail("completed attempt requires stop finish reason")
    if state in {"failed", "aborted"} and row["finish_reason"] is not None:
        _fail("terminal failed attempt has a finish reason")
    if failure is not None:
        failure = _exact_mapping(failure, {"category", "message"}, "attempt failure")
        if _member(failure["category"], {"generation-failed", "generation-aborted", "generation-incomplete"}, "attempt failure category") != expected_failure or failure["message"] != SAFE_FAILURE_MESSAGE:
            _fail("attempt failure is invalid")
        if expected_failure is None:
            _fail("completed attempt has a failure")
    token_values: list[int | None] = []
    for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens"):
        if row[key] is not None:
            token_values.append(_integer(row[key], f"attempt {key}"))
        else:
            token_values.append(None)
    if row["known_cost_usd"] is not None:
        _decimal(row["known_cost_usd"], "attempt known cost")
    if row["remote_outcome_unknown"] is not None:
        _boolean(row["remote_outcome_unknown"], "attempt remote outcome flag")
    provenance = _validate_provenance(
        row["request_time_provenance"],
        settings_provenance_values=settings_provenance_values,
    )
    _validate_remote_outcome(row, state, provenance)
    attribution = provenance.get("attribution")
    if attribution is None:
        if (row["backend_id"], row["provider_id"], row["model"]) != ("[unavailable]", None, None):
            _fail("attempt attribution contradicts unavailable provenance")
    elif not isinstance(attribution, Mapping) or (
        row["backend_id"], row["provider_id"], row["model"]
    ) != (attribution["backend_id"], attribution["provider_id"], attribution["model"]):
        _fail("attempt attribution contradicts request-time provenance")
    if row["returned_model"] is not None:
        if row["returned_model"] == SAFE_REDACTED_METADATA:
            pass
        elif row["model"] is not None and row["returned_model"] != row["model"]:
            _fail("attempt returned model is not safe attribution")
        else:
            _available_metadata(row["returned_model"], _safe_provider_model_id, "attempt returned model")
    return row


def _validate_remote_outcome(
    row: Mapping[str, object], state: str, provenance: Mapping[str, object],
) -> None:
    """Apply only the landed outcome rules identified by archived provenance."""
    remote_unknown = row["remote_outcome_unknown"]
    if provenance["status"] == "available":
        # Snapshot versions 2 and 3 are the landed v2/v3 provider language.
        if state == "complete" and remote_unknown is not False:
            _fail("v2/v3 complete attempt has an unknown remote outcome")
        return
    if provenance["status"] != "legacy-limited":
        return
    attribution = provenance["attribution"]
    if (
        attribution["provider_id_status"] != "available"
        or attribution["provider_id"] not in {"local_openai", "openrouter"}
    ):
        return
    # Legacy records with Phase 3 provider attribution retain Phase 3's
    # explicit outcome truth. Other legacy/null forms remain valid as-is.
    if remote_unknown is None:
        _fail("Phase 3 attempt lacks remote outcome truth")
    if row["finish_reason"] is not None and remote_unknown is not False:
        _fail("Phase 3 known finish has an unknown remote outcome")
    if state == "complete" and remote_unknown is not False:
        _fail("Phase 3 complete attempt has an unknown remote outcome")


def _validate_configuration(value: object) -> None:
    configuration = _exact_mapping(value, {"semantic", "selection", "overrides"}, "chat configuration")
    if configuration["semantic"] != "inert-continuation-hints" or type(configuration["overrides"]) is not list:
        _fail("chat configuration is invalid")
    if configuration["selection"] is not None:
        selection = _exact_mapping(configuration["selection"], {"model", "selection_required", "revision"}, "chat selection")
        selection_required = _boolean(selection["selection_required"], "chat selection required")
        _integer(selection["revision"], "chat selection revision", minimum=1)
        if (selection_required and selection["model"] is not None) or (not selection_required and selection["model"] is None):
            _fail("chat selection contradicts required state")
        if selection["model"] is not None:
            selection_descriptor = _validate_descriptor(selection["model"])
        else:
            selection_descriptor = None
    else:
        selection_descriptor = None
    override_ids: set[str] = set()
    descriptors: list[Mapping[str, object]] = []
    if selection_descriptor is not None:
        descriptors.append(selection_descriptor)
    for override in configuration["overrides"]:
        override = _exact_mapping(override, {"model", "revision", "temperature", "max_output_tokens", "reasoning_effort", "timeout_seconds"}, "chat override")
        descriptor = _validate_descriptor(override["model"])
        model_id = descriptor["source_model_entry_id"]
        if model_id in override_ids:
            _fail("chat configuration has duplicate model override")
        override_ids.add(model_id)
        descriptors.append(descriptor)
        _integer(override["revision"], "chat override revision", minimum=1)
        if override["temperature"] is not None and (type(override["temperature"]) not in {int, float} or type(override["temperature"]) is bool or not math.isfinite(override["temperature"]) or not 0 <= override["temperature"] <= 2):
            _fail("chat override temperature is invalid")
        if override["max_output_tokens"] is not None:
            _integer(override["max_output_tokens"], "chat override maximum output", minimum=1)
        if _none_or_member(override["reasoning_effort"], {"none"}, "chat override reasoning effort") not in {None, "none"} or (override["timeout_seconds"] is not None and (type(override["timeout_seconds"]) not in {int, float} or type(override["timeout_seconds"]) is bool or not math.isfinite(override["timeout_seconds"]) or override["timeout_seconds"] <= 0)):
            _fail("chat override is invalid")
    _validate_configuration_descriptor_joins(descriptors)


def _validate_configuration_descriptor_joins(descriptors: list[Mapping[str, object]]) -> None:
    """Bind portable descriptors only where the archive exposes an identity."""
    by_model: dict[str, Mapping[str, object]] = {}
    by_connection: dict[str, tuple[object, ...]] = {}
    connection_fields = (
        "source_connection_id", "source_connection_id_status", "backend_type", "backend_type_status",
        "provider_profile", "provider_profile_status", "connection_revision", "catalogue_revision",
    )
    for descriptor in descriptors:
        model_id = descriptor["source_model_entry_id"]
        if descriptor["source_model_entry_id_status"] == "available":
            existing = by_model.setdefault(model_id, descriptor)
            if existing != descriptor:
                _fail("chat configuration model descriptor contradicts itself")
        connection_id = descriptor["source_connection_id"]
        if descriptor["source_connection_id_status"] == "available":
            facts = tuple(descriptor[key] for key in connection_fields)
            existing_facts = by_connection.setdefault(connection_id, facts)
            if existing_facts != facts:
                _fail("chat configuration connection descriptor contradicts itself")


def _validate_descriptor(value: object) -> Mapping[str, object]:
    text_fields = {"source_model_entry_id", "source_connection_id", "backend_type", "provider_profile", "provider_model_id", "display_name", "origin", "availability"}
    fields = text_fields | {f"{key}_status" for key in text_fields} | {"model_revision", "connection_revision", "catalogue_revision"}
    descriptor = _exact_mapping(value, fields, "portable model descriptor")
    projectors = {
        "source_model_entry_id": _safe_installation_id,
        "source_connection_id": _safe_installation_id,
        "backend_type": lambda item: _safe_closed_metadata(item, frozenset({"fake", "openai_compatible_http"})),
        "provider_profile": lambda item: _safe_closed_metadata(item, frozenset({"generic", "openrouter"})),
        "provider_model_id": _safe_provider_model_id,
        "display_name": _safe_display_metadata,
        "origin": lambda item: _safe_closed_metadata(item, frozenset({"manual", "discovered", "manual_confirmed"})),
        "availability": lambda item: _safe_closed_metadata(item, frozenset({"available", "unavailable", "stale", "disconnected"})),
    }
    for key in text_fields:
        _text(descriptor[key], f"portable descriptor {key}")
        _status_metadata(descriptor[key], descriptor[f"{key}_status"], projectors[key], f"portable descriptor {key}")
    for key in ("model_revision", "connection_revision", "catalogue_revision"):
        _integer(descriptor[key], f"portable descriptor {key}", minimum=1 if key in {"model_revision", "connection_revision"} else 0)
    return descriptor


def _validate_attachment(value: object) -> Mapping[str, object]:
    fields = {"source_id", "blob_digest", "filename", "filename_status", "source_kind", "source_kind_status", "text_representation_id", "text_digest", "text_eligibility", "ineligibility_reason", "created_at", "byte_size", "integrity_status"}
    row = _exact_mapping(value, fields, "attachment")
    _text(row["source_id"], "attachment ID")
    _digest(row["blob_digest"], "attachment digest")
    for key in ("filename", "source_kind"):
        _text(row[key], f"attachment {key}", allow_empty=True)
    _status_metadata(row["filename"], row["filename_status"], _safe_attachment_filename, "attachment filename")
    _status_metadata(
        row["source_kind"], row["source_kind_status"],
        lambda item: _safe_closed_metadata(item, frozenset({"filesystem"})),
        "attachment source kind",
    )
    if row["text_representation_id"] is None:
        if row["text_digest"] is not None or row["text_eligibility"] != "unavailable":
            _fail("attachment text representation is invalid")
    else:
        _digest(row["text_representation_id"], "attachment representation ID")
        if row["text_digest"] != row["text_representation_id"] or row["text_eligibility"] != "eligible":
            _fail("attachment text representation is invalid")
    _none_or_member(row["ineligibility_reason"], {"not_text", "invalid_utf8", "contains_nul"}, "attachment ineligibility reason")
    _timestamp(row["created_at"], "attachment timestamp")
    _integer(row["byte_size"], "attachment byte size")
    if row["integrity_status"] != "verified":
        _fail("archive attachment is not independently verified")
    return row


def _validate_graph(contents: Mapping[str, bytes], manifest: Mapping[str, object]) -> None:
    chat = _validate_chat(_json_object(contents["domain/chat.json"], "chat"))
    if manifest["source_chat"] != {"source_id": chat["source_id"], "title": chat["title"]}:
        _fail("manifest source chat contradicts domain chat")
    messages = [_validate_message(item, chat["source_id"]) for item in _rows(contents, "domain/messages.jsonl")]
    by_message: dict[str, Mapping[str, object]] = {}
    sequences: set[int] = set()
    for row in messages:
        if row["source_id"] in by_message or row["sequence"] in sequences:
            _fail("message identity or sequence is duplicated")
        by_message[row["source_id"]] = row
        sequences.add(row["sequence"])
    if messages and sequences != set(range(1, len(messages) + 1)):
        _fail("message sequence is not contiguous")
    for row in messages:
        parent = row["parent_id"]
        if parent is not None:
            if parent not in by_message or parent == row["source_id"] or by_message[parent]["sequence"] >= row["sequence"]:
                _fail("message parent reference is invalid")
        supersedes = row["supersedes_id"]
        if supersedes is None:
            if row["revision"] != 1:
                _fail("message without supersession must be revision one")
        else:
            previous = by_message.get(supersedes)
            if previous is None or supersedes == row["source_id"] or previous["role"] != row["role"] or previous["lineage_id"] != row["lineage_id"] or previous["revision"] + 1 != row["revision"]:
                _fail("message supersession is invalid")
    for lineage in {row["lineage_id"] for row in messages}:
        revisions = sorted(row["revision"] for row in messages if row["lineage_id"] == lineage)
        if revisions != list(range(1, len(revisions) + 1)):
            _fail("message lineage revisions are invalid")
    for start in by_message:
        seen: set[str] = set()
        current: str | None = start
        while current is not None:
            if current in seen:
                _fail("message parent graph contains a cycle")
            seen.add(current)
            current = by_message[current]["parent_id"]
    head = chat["head_message_id"]
    if head is not None:
        head_row = by_message.get(head)
        if head_row is None or head_row["role"] != "assistant" or any(row["parent_id"] == head for row in messages):
            _fail("chat head is not an assistant leaf")
    elif messages:
        _fail("nonempty chat lacks a head")
    attempts = [_validate_attempt(item, chat["source_id"], by_message) for item in _rows(contents, "domain/attempts.jsonl")]
    by_attempt: dict[str, Mapping[str, object]] = {}
    assistant_owner: set[str] = set()
    for row in attempts:
        if row["source_id"] in by_attempt or row["assistant_message_id"] in assistant_owner:
            _fail("attempt identity or assistant ownership is duplicated")
        by_attempt[row["source_id"]] = row
        assistant_owner.add(row["assistant_message_id"])
    if any(row["role"] == "assistant" and row["source_id"] not in assistant_owner for row in messages):
        _fail("assistant message lacks its generation attempt")
    contexts = _rows(contents, "domain/context-plans.jsonl")
    context_attempts: set[str] = set()
    for context in contexts:
        fields = {"attempt_id", "plan_version", "canonical_digest", "wire_representation_digest", "budget_limit", "budget_semantics", "adapter_id", "adapter_version", "input_counts", "envelope_overhead", "output_reserve", "input_units", "total_units", "headroom", "created_at"}
        context = _exact_mapping(context, fields, "persisted context plan")
        attempt_id = _text(context["attempt_id"], "context attempt ID")
        if attempt_id in context_attempts or attempt_id not in by_attempt or context["plan_version"] != 3 or type(context["plan_version"]) is not int:
            _fail("context plan ownership is invalid")
        context_attempts.add(attempt_id)
        provenance = by_attempt[attempt_id]["request_time_provenance"]
        if provenance.get("status") != "available" or provenance.get("snapshot_version") != 3:
            _fail("context plan does not belong to a v3 attempt")
        safe = _validate_safe_context(provenance["context"])
        for key, safe_key in (("canonical_digest", "canonical_digest"), ("wire_representation_digest", "wire_representation_sha256"), ("budget_limit", None), ("budget_semantics", None), ("adapter_id", None), ("adapter_version", None), ("input_counts", "input_counts"), ("envelope_overhead", None), ("output_reserve", None), ("input_units", None), ("total_units", None), ("headroom", None)):
            expected = safe[safe_key] if safe_key is not None else safe["budget"][key.removeprefix("budget_")]
            if context[key] != expected or type(context[key]) is not type(expected):
                _fail("persisted context plan contradicts safe request-time provenance")
        _timestamp(context["created_at"], "persisted context timestamp", milliseconds=True)
    for row in attempts:
        provenance = row["request_time_provenance"]
        if provenance.get("status") == "available" and provenance.get("snapshot_version") == 3 and row["source_id"] not in context_attempts:
            _fail("v3 attempt lacks its persisted context plan")
    attachments = [_validate_attachment(item) for item in _rows(contents, "domain/attachments.jsonl")]
    by_attachment: dict[str, Mapping[str, object]] = {}
    for row in attachments:
        if row["source_id"] in by_attachment:
            _fail("attachment identity is duplicated")
        by_attachment[row["source_id"]] = row
    message_references = _validate_relations(_rows(contents, "domain/message-attachments.jsonl"), "message_id", by_message, by_attachment)
    attempt_references = _validate_relations(_rows(contents, "domain/attempt-attachments.jsonl"), "attempt_id", by_attempt, by_attachment)
    referenced_attachments = set().union(*message_references.values(), *attempt_references.values())
    if set(by_attachment) != referenced_attachments:
        _fail("archive contains an unreferenced attachment")
    for attempt_id, attempt in by_attempt.items():
        provenance = attempt["request_time_provenance"]
        if provenance.get("status") == "available" and provenance.get("snapshot_version") == 3:
            context = provenance["context"]
            allowed_digests = {by_attachment[attachment_id]["blob_digest"] for attachment_id in attempt_references.get(attempt_id, set())}
            for source in context["sources"]:
                if source["kind"] == "attachment" and source["selected"] and source["representation_digest"] not in allowed_digests:
                    _fail("selected context attachment is not related to its attempt")
    _validate_configuration(_json_object(contents["domain/chat-configuration.json"], "chat configuration"))
    provenance = _exact_mapping(_json_object(contents["domain/provenance.json"], "provenance"), {"source_origin", "import_origin", "attachment_policy", "resources"}, "provenance")
    if provenance["source_origin"] != "native" or provenance["import_origin"] != "not-recorded" or provenance["attachment_policy"] != manifest["attachment_policy"] or type(provenance["resources"]) is not list:
        _fail("archive provenance is invalid")
    _validate_attachment_resources(manifest, contents, attachments, provenance["resources"])


def _validate_relations(rows: tuple[Mapping[str, object], ...], owner_key: str, owners: Mapping[str, Mapping[str, object]], attachments: Mapping[str, Mapping[str, object]]) -> dict[str, set[str]]:
    seen: set[tuple[str, str]] = set()
    ordinals: dict[str, list[int]] = {}
    referenced: set[str] = set()
    by_owner: dict[str, set[str]] = {}
    for row in rows:
        row = _exact_mapping(row, {owner_key, "attachment_id", "ordinal"}, "attachment relation")
        owner = _text(row[owner_key], f"attachment relation {owner_key}")
        attachment = _text(row["attachment_id"], "attachment relation attachment ID")
        ordinal = _integer(row["ordinal"], "attachment relation ordinal")
        if owner not in owners or attachment not in attachments or (owner, attachment) in seen:
            _fail("attachment relation is invalid")
        seen.add((owner, attachment)); referenced.add(attachment)
        by_owner.setdefault(owner, set()).add(attachment)
        ordinals.setdefault(owner, []).append(ordinal)
    for values in ordinals.values():
        if sorted(values) != list(range(len(values))):
            _fail("attachment relation ordinals are not contiguous")
    return by_owner


def _validate_attachment_resources(manifest: Mapping[str, object], contents: Mapping[str, bytes], attachments: list[Mapping[str, object]], provenance_resources: list[object]) -> None:
    declared: dict[str, int] = {}
    for attachment in attachments:
        digest, size = attachment["blob_digest"], attachment["byte_size"]
        if digest in declared and declared[digest] != size:
            _fail("same attachment digest has contradictory sizes")
        declared[digest] = size
    policy = manifest["attachment_policy"]
    external = manifest["external_resources"]
    if policy == "embedded":
        payloads = {name.removeprefix("payloads/sha256/"): value for name, value in contents.items() if name.startswith("payloads/sha256/")}
        if set(payloads) != set(declared):
            _fail("embedded payload set does not match attachments")
        for digest, raw in payloads.items():
            if _DIGEST.fullmatch(digest) is None or sha256_hex(raw) != digest or len(raw) != declared[digest]:
                _fail("embedded payload integrity is invalid")
        if provenance_resources:
            _fail("embedded archive provenance resources are invalid")
    else:
        if type(provenance_resources) is not list or len(external) != len(declared) or len(provenance_resources) != len(declared):
            _fail("external attachment resource declarations are incomplete")
        external_by_digest = {item["digest"]: item for item in external}
        provenance_by_digest = {item.get("digest"): item for item in provenance_resources if type(item) is dict}
        if set(external_by_digest) != set(declared) or set(provenance_by_digest) != set(declared):
            _fail("external attachment resource declarations are invalid")
        for digest, size in declared.items():
            item = external_by_digest[digest]
            if item["size"] != size or provenance_by_digest[digest] != {"digest": digest, "size": size, "required": True}:
                _fail("external attachment resource declarations contradict attachments")
