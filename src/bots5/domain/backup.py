"""Closed Backup v1 manifest and result models.

Backup v1 is an installation recovery artifact.  It is deliberately separate
from Archive v1/v2 chat interchange and has no restore semantics here.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid as _uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Mapping
from uuid import UUID


BACKUP_FORMAT = "org.necromilias.bots5.installation-backup"
BACKUP_VERSION = 1
BOUNDARY_REVISION = 1
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
SECRET_EXCLUSION = "provider-credential-values-and-secret-store-material-excluded"
FEATURES = ("content-addressed-attachments", "sqlite-state")
LOGICAL_ROLES = frozenset({
    "database", "attachment-payload", "completion", "config", "state", "log",
    "attachment-recovery",
})

MAX_MANIFEST_BYTES = 256 * 1024 * 1024
MAX_ENTRY_COUNT = 1_000_000
MAX_ENTRY_BYTES = 1 << 40
MAX_TOTAL_UNCOMPRESSED_BYTES = 4 << 40
MAX_SQLITE_BYTES = 64 << 30
MAX_SCRATCH_BYTES = MAX_SQLITE_BYTES + (1 << 30)
MAX_JSON_DEPTH = 64
MAX_PATH_BYTES = 1024
MAX_PATH_DEPTH = 32
MAX_VERIFICATION_SECONDS = 6 * 60 * 60

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PAYLOAD_PATH = re.compile(r"payloads/sha256/[0-9a-f]{64}\Z")
_RECOVERY_PATH = re.compile(
    r"recovery-artifacts/attachments/(staging|captures|gc)/"
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}|[0-9a-f]{64})\Z"
)
_CANONICAL_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class BackupManifestError(ValueError):
    """Backup v1 manifest data is not the accepted closed language."""


class BackupManifestUnsupported(BackupManifestError):
    """A manifest declares a Backup v1 version this build cannot verify."""


class BackupEntryRole(str, Enum):
    DATABASE = "database"
    ATTACHMENT_PAYLOAD = "attachment-payload"
    COMPLETION = "completion"
    CONFIG = "config"
    STATE = "state"
    LOG = "log"
    ATTACHMENT_RECOVERY = "attachment-recovery"


class BackupClassification(str, Enum):
    REQUIRED_BACKUP = "required-backup"
    DERIVED_REBUILDABLE = "derived-rebuildable"
    OPTIONAL_DIAGNOSTIC = "optional-diagnostic"
    SECRET_EXCLUDED = "secret-excluded"
    OUTSIDE_INSTALLATION_RECOVERY = "outside-installation-recovery"


@dataclass(frozen=True, slots=True)
class BackupRootModel:
    data_root_is_override: bool
    config_root_is_override: bool
    state_root_is_override: bool


@dataclass(frozen=True, slots=True)
class BackupCaptureFacts:
    journal_mode: str
    synchronous: int
    foreign_keys: bool
    source_database_size: int
    source_database_sha256: str
    ready_blob_count: int
    staging_blob_count: int
    deleting_blob_count: int
    required_payload_count: int
    search_source_revision: int
    root_model: BackupRootModel


@dataclass(frozen=True, slots=True)
class BackupScopeEntry:
    component: str
    classification: BackupClassification
    included: bool


@dataclass(frozen=True, slots=True)
class BackupManifestEntry:
    path: str
    media_type: str
    logical_role: str
    required: bool
    uncompressed_size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BackupManifest:
    backup_id: str
    created_at: str
    source_application_version: str
    source_db_migration_revision: str
    capture: BackupCaptureFacts
    scope: tuple[BackupScopeEntry, ...]
    entry_inventory: tuple[BackupManifestEntry, ...]

    def canonical_bytes(self) -> bytes:
        return canonical_backup_json(backup_manifest_object(self))


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    format: str
    receipt_version: int
    verification_id: str
    verified_at: str
    backup_id: str
    backup_logical_content_digest: str
    artifact_size: int
    artifact_sha256: str
    source_db_migration_revision: str
    verifier_application_version: str
    sqlite_runtime_version: str
    outcome: str
    passed_checks: tuple[str, ...]
    failed_check_ids: tuple[str, ...]
    reason_code: str | None

    def canonical_bytes(self) -> bytes:
        return canonical_backup_json(verification_receipt_object(self))

    def canonical_object(self) -> dict[str, object]:
        return verification_receipt_object(self)


def verification_receipt_object(receipt: BackupReceipt) -> dict[str, object]:
    return {
        "format": receipt.format,
        "receipt_version": receipt.receipt_version,
        "verification_id": receipt.verification_id,
        "verified_at": receipt.verified_at,
        "backup_id": receipt.backup_id,
        "backup_logical_content_digest": receipt.backup_logical_content_digest,
        "artifact_size": receipt.artifact_size,
        "artifact_sha256": receipt.artifact_sha256,
        "source_db_migration_revision": receipt.source_db_migration_revision,
        "verifier_application_version": receipt.verifier_application_version,
        "sqlite_runtime_version": receipt.sqlite_runtime_version,
        "outcome": receipt.outcome,
        "passed_checks": list(receipt.passed_checks),
        "failed_check_ids": list(receipt.failed_check_ids),
        "reason_code": receipt.reason_code,
    }


def canonical_backup_json(value: object) -> bytes:
    """Return canonical, LF-terminated JSON without duplicate or NaN values."""
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return text.encode("utf-8") + b"\n"


def logical_content_digest(entries: tuple[BackupManifestEntry, ...]) -> str:
    rows = sorted(entries, key=lambda item: item.path)
    payload = "".join(
        f"{item.path}\t{item.uncompressed_size}\t{item.sha256}\n"
        for item in rows
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def backup_manifest_object(manifest: BackupManifest) -> dict[str, object]:
    capture = {
        "journal_mode": manifest.capture.journal_mode,
        "synchronous": manifest.capture.synchronous,
        "foreign_keys": manifest.capture.foreign_keys,
        "source_database_size": manifest.capture.source_database_size,
        "source_database_sha256": manifest.capture.source_database_sha256,
        "attachment_blob_state_counts": {
            "ready": manifest.capture.ready_blob_count,
            "staging": manifest.capture.staging_blob_count,
            "deleting": manifest.capture.deleting_blob_count,
        },
        "required_payload_count": manifest.capture.required_payload_count,
        "search_source_revision": manifest.capture.search_source_revision,
        "root_model": {
            "data_root_is_override": manifest.capture.root_model.data_root_is_override,
            "config_root_is_override": manifest.capture.root_model.config_root_is_override,
            "state_root_is_override": manifest.capture.root_model.state_root_is_override,
        },
    }
    return {
        "format": BACKUP_FORMAT,
        "backup_version": BACKUP_VERSION,
        "backup_id": manifest.backup_id,
        "created_at": manifest.created_at,
        "source_application_version": manifest.source_application_version,
        "source_db_migration_revision": manifest.source_db_migration_revision,
        "boundary_revision": BOUNDARY_REVISION,
        "capture": capture,
        "scope": [
            {
                "component": item.component,
                "classification": item.classification.value,
                "included": item.included,
            }
            for item in manifest.scope
        ],
        "entry_inventory": [
            {
                "path": item.path,
                "media_type": item.media_type,
                "logical_role": item.logical_role,
                "required": item.required,
                "uncompressed_size": item.uncompressed_size,
                "sha256": item.sha256,
            }
            for item in manifest.entry_inventory
        ],
        "declared_total_uncompressed_bytes": sum(
            item.uncompressed_size for item in manifest.entry_inventory
        ),
        "logical_content_digest": logical_content_digest(manifest.entry_inventory),
        "secret_exclusion": SECRET_EXCLUSION,
        "features": list(FEATURES),
    }


def canonical_utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise BackupManifestError("backup timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def validate_backup_id(value: str) -> str:
    if type(value) is not str:
        raise BackupManifestError("backup id is malformed")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise BackupManifestError("backup id is malformed") from exc
    if parsed.variant != _uuid.RFC_4122:
        raise BackupManifestError("backup id is not RFC 4122 variant")
    if parsed.version != 7 or str(parsed) != value:
        raise BackupManifestError("backup id is not canonical lowercase UUIDv7")
    return value


def validate_sha256(value: str, label: str = "entry digest") -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise BackupManifestError(f"{label} is not lowercase SHA-256")
    return value


def validate_timestamp(value: str) -> str:
    if type(value) is not str or not value.endswith("Z"):
        raise BackupManifestError("backup timestamp is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise BackupManifestError("backup timestamp is not canonical UTC") from exc
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise BackupManifestError("backup timestamp is not canonical UTC")
    if canonical_utc_timestamp(parsed) != value:
        raise BackupManifestError("backup timestamp is not canonical")
    return value


def classify_entry_path(path: str) -> str:
    role = _entry_role(path)
    if role is None:
        raise BackupManifestError("backup entry path is outside the closed grammar")
    return role


def entry_media_type(path: str, role: str) -> str:
    if path == "database/state.sqlite3":
        return "application/vnd.sqlite3"
    if role == "attachment-payload":
        return "application/octet-stream"
    if role == "completion":
        return "application/octet-stream"
    if role == "attachment-recovery":
        return "application/octet-stream"
    if role == "log":
        return "text/plain"
    if path.endswith(".json"):
        return "application/json"
    return "application/octet-stream"


def _entry_role(path: str) -> str | None:
    if path == "database/state.sqlite3":
        return "database"
    if _PAYLOAD_PATH.fullmatch(path):
        return "attachment-payload"
    if path == "COMPLETED":
        return "completion"
    if _RECOVERY_PATH.fullmatch(path):
        return "attachment-recovery"
    if path.startswith("config/"):
        return _relative_role(path, "config")
    if path.startswith("state/"):
        return _relative_role(path, "state")
    if path.startswith("logs/"):
        return _relative_role(path, "log")
    return None


def _relative_role(path: str, role: str) -> str | None:
    parts = path.split("/")[1:]
    if not parts or any(
        part in {"", ".", ".."} or _CANONICAL_COMPONENT.fullmatch(part) is None
        for part in parts
    ):
        return None
    return role


def validate_entry_path(path: str) -> str:
    if type(path) is not str or not path or "\x00" in path or "\\" in path:
        raise BackupManifestError("backup entry path is malformed")
    if path.startswith("/") or path.endswith("/"):
        raise BackupManifestError("backup entry path is malformed")
    if len(path.encode("utf-8")) > MAX_PATH_BYTES:
        raise BackupManifestError("backup entry path exceeds bounded length")
    if path.count("/") >= MAX_PATH_DEPTH:
        raise BackupManifestError("backup entry path exceeds bounded depth")
    if path == "manifest.json":
        return path
    classify_entry_path(path)
    return path


def reject_path_collisions(paths: list[str]) -> None:
    keys = {
        unicodedata.normalize("NFC", path).casefold()
        for path in paths
    }
    if len(keys) != len(paths):
        raise BackupManifestError("backup paths contain a Unicode-normalisation collision")


def default_backup_scope(recovery_artifacts_included: bool) -> tuple[BackupScopeEntry, ...]:
    return (
        BackupScopeEntry("database", BackupClassification.REQUIRED_BACKUP, True),
        BackupScopeEntry(
            "attachment-payloads", BackupClassification.REQUIRED_BACKUP, True
        ),
        BackupScopeEntry(
            "attachment-recovery-artifacts",
            BackupClassification.REQUIRED_BACKUP,
            recovery_artifacts_included,
        ),
        BackupScopeEntry("config", BackupClassification.REQUIRED_BACKUP, False),
        BackupScopeEntry("state", BackupClassification.REQUIRED_BACKUP, False),
        BackupScopeEntry("logs", BackupClassification.OPTIONAL_DIAGNOSTIC, False),
        BackupScopeEntry("cache", BackupClassification.DERIVED_REBUILDABLE, False),
        BackupScopeEntry("secrets", BackupClassification.SECRET_EXCLUDED, False),
        BackupScopeEntry(
            "campaign-repository-evidence",
            BackupClassification.OUTSIDE_INSTALLATION_RECOVERY,
            False,
        ),
        BackupScopeEntry(
            "unrelated-filesystem",
            BackupClassification.OUTSIDE_INSTALLATION_RECOVERY,
            False,
        ),
    )


def _scope_component(path: str) -> str | None:
    if path == "database/state.sqlite3":
        return "database"
    if path.startswith("payloads/sha256/"):
        return "attachment-payloads"
    if path.startswith("recovery-artifacts/attachments/"):
        return "attachment-recovery-artifacts"
    for component in ("config", "state", "logs"):
        if path.startswith(f"{component}/"):
            return component
    return None


def validate_manifest_object(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "format", "backup_version", "backup_id", "created_at",
        "source_application_version", "source_db_migration_revision",
        "boundary_revision", "capture", "scope", "entry_inventory",
        "declared_total_uncompressed_bytes", "logical_content_digest",
        "secret_exclusion", "features",
    }:
        raise BackupManifestError("backup manifest is not a closed schema")
    if value["format"] != BACKUP_FORMAT:
        raise BackupManifestError("backup format is unsupported")
    backup_version = value["backup_version"]
    boundary_revision = value["boundary_revision"]
    if (
        (type(backup_version) is int and backup_version > BACKUP_VERSION)
        or (type(boundary_revision) is int and boundary_revision > BOUNDARY_REVISION)
    ):
        raise BackupManifestUnsupported("backup version is unsupported")
    if (
        type(backup_version) is not int
        or type(boundary_revision) is not int
        or backup_version != BACKUP_VERSION
        or boundary_revision != BOUNDARY_REVISION
    ):
        raise BackupManifestError("backup version is unsupported")
    validate_backup_id(str(value["backup_id"]))
    validate_timestamp(str(value["created_at"]))
    if type(value["source_application_version"]) is not str or not value["source_application_version"]:
        raise BackupManifestError("backup source application version is malformed")
    if type(value["source_db_migration_revision"]) is not str or not value["source_db_migration_revision"]:
        raise BackupManifestError("backup database revision is malformed")
    inventory = _entry_inventory(value["entry_inventory"])
    recovery_included = any(
        str(item["path"]).startswith("recovery-artifacts/attachments/")
        for item in inventory
    )
    capture = _capture_object(value["capture"], inventory=inventory)
    _scope_object(value["scope"], inventory=inventory, recovery_included=recovery_included)
    if value["declared_total_uncompressed_bytes"] != sum(item["uncompressed_size"] for item in inventory):
        raise BackupManifestError("backup declared total bytes are inconsistent")
    if value["logical_content_digest"] != logical_content_digest_from_rows(inventory):
        raise BackupManifestError("backup logical content digest is inconsistent")
    if value["secret_exclusion"] != SECRET_EXCLUSION:
        raise BackupManifestError("backup secret exclusion declaration is invalid")
    if value["features"] != list(FEATURES):
        raise BackupManifestError("backup feature declaration is invalid")
    if capture["source_database_size"] > MAX_SQLITE_BYTES:
        raise BackupManifestError("backup SQLite size exceeds bounded limit")
    return value


def logical_content_digest_from_rows(rows: list[dict[str, object]]) -> str:
    payload = "".join(
        f"{row['path']}\t{row['uncompressed_size']}\t{row['sha256']}\n"
        for row in sorted(rows, key=lambda item: str(item["path"]))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _capture_object(value: object, *, inventory: list[dict[str, object]]) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "journal_mode", "synchronous", "foreign_keys", "source_database_size",
        "source_database_sha256", "attachment_blob_state_counts",
        "required_payload_count", "search_source_revision", "root_model",
    }:
        raise BackupManifestError("backup capture facts are not a closed schema")
    if value["journal_mode"] != "delete" or value["synchronous"] != 2 or value["foreign_keys"] is not True:
        raise BackupManifestError("backup SQLite durability facts are unsupported")
    if type(value["source_database_size"]) is not int or value["source_database_size"] < 1:
        raise BackupManifestError("backup SQLite size is malformed")
    validate_sha256(str(value["source_database_sha256"]), "capture source digest")
    counts = value["attachment_blob_state_counts"]
    if type(counts) is not dict or set(counts) != {"ready", "staging", "deleting"} or any(
        type(item) is not int or item < 0 for item in counts.values()
    ):
        raise BackupManifestError("backup attachment counts are malformed")
    for name in ("required_payload_count", "search_source_revision"):
        if type(value[name]) is not int or value[name] < 0:
            raise BackupManifestError(f"backup {name} is malformed")
    captured_payload_count = sum(
        1
        for row in inventory
        if _PAYLOAD_PATH.fullmatch(str(row["path"])) is not None
    )
    if value["required_payload_count"] != captured_payload_count:
        raise BackupManifestError("backup payload count contradicts captured inventory")
    root_model = value["root_model"]
    if type(root_model) is not dict or set(root_model) != {
        "data_root_is_override", "config_root_is_override", "state_root_is_override"
    } or any(type(item) is not bool for item in root_model.values()):
        raise BackupManifestError("backup root model is malformed")
    recovery_included = any(
        str(row["path"]).startswith("recovery-artifacts/attachments/")
        for row in inventory
    )
    if recovery_included and value["required_payload_count"] < 0:
        raise BackupManifestError("backup recovery capture is malformed")
    return dict(value)


def _scope_object(value: object, *, inventory: list[dict[str, object]], recovery_included: bool) -> None:
    expected = backup_manifest_scope_rows(recovery_included)
    if value != expected:
        raise BackupManifestError("backup scope declaration is not closed")
    included = {str(row["component"]) for row in value if row["included"] is True}
    declared = {_scope_component(str(row["path"])) for row in inventory}
    declared.discard(None)
    unexpected = declared - included
    if unexpected:
        raise BackupManifestError("backup scope contradicts its inventory")


def backup_manifest_scope_rows(recovery_included: bool) -> list[dict[str, object]]:
    return [
        {
            "component": item.component,
            "classification": item.classification.value,
            "included": item.included,
        }
        for item in default_backup_scope(recovery_included)
    ]


def _entry_inventory(value: object) -> list[dict[str, object]]:
    if type(value) is not list or len(value) > MAX_ENTRY_COUNT:
        raise BackupManifestError("backup entry inventory is malformed")
    rows: list[dict[str, object]] = []
    paths: list[str] = []
    for row in value:
        if type(row) is not dict or set(row) != {
            "path", "media_type", "logical_role", "required",
            "uncompressed_size", "sha256",
        }:
            raise BackupManifestError("backup inventory row is malformed")
        path = validate_entry_path(str(row["path"]))
        role = classify_entry_path(path)
        if row["logical_role"] != role or row["required"] is not True:
            raise BackupManifestError("backup inventory role or requirement is invalid")
        size = row["uncompressed_size"]
        if type(size) is not int or size < 0 or size > MAX_ENTRY_BYTES:
            raise BackupManifestError("backup inventory size is malformed")
        if path == "COMPLETED" and size != 0:
            raise BackupManifestError("backup completion marker must be zero length")
        if size == 0 and row["sha256"] != EMPTY_SHA256:
            raise BackupManifestError("backup empty-entry digest is invalid")
        if size > 0 and _DIGEST.fullmatch(str(row["sha256"])) is None:
            raise BackupManifestError("backup inventory digest is malformed")
        if role == "attachment-payload":
            digest_text = path.removeprefix("payloads/sha256/")
            if row["sha256"] != digest_text:
                raise BackupManifestError("payload path and digest disagree")
        validate_sha256(str(row["sha256"]))
        expected_media = entry_media_type(path, role)
        if row["media_type"] != expected_media:
            raise BackupManifestError("backup inventory media type is invalid")
        rows.append(row)
        paths.append(path)
    reject_path_collisions(paths)
    if not paths or paths[-1] != "COMPLETED":
        raise BackupManifestError("backup completion marker must be final")
    if paths[0] == "COMPLETED":
        raise BackupManifestError("backup completion marker must not be first")
    if paths.count("database/state.sqlite3") != 1:
        raise BackupManifestError("backup must contain exactly one SQLite snapshot")
    return rows


def validate_manifest_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return validate_manifest_object(value)
