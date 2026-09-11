"""Crash-recoverable, authority-derived Phase 6 migration.

No database or migration pathname crosses this module's public boundary. All
mutable filesystem access is relative to descriptors retained by
``DataRootAuthority`` and every SQLite open uses the rooted native VFS.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool
from uuid6 import uuid7

from bots5.core.errors import AuthorityError
from bots5.infrastructure.data_root_authority import (
    DataRootAuthority,
    FileIdentity,
    _check_regular,
    _identity,
)
from bots5.infrastructure.persistence.transition_guard import install_transition_guard
from bots5.infrastructure.rooted_sqlite_vfs import (
    RootedSQLiteVfs,
    RootedVfsRegistrationCloseUnknown,
)


_HEAD = "0009_phase6_context_attachments"
_PRIOR_REVISIONS = (
    "0001_desktop_state",
    "0002_conversation_lineage",
    "0003_integrity_boundaries",
    "0004_integrity_guard_function",
    "0005_generation_outcomes",
    "0006_phase4_workspace",
    "0007_phase5_provider_model_configuration",
    "0008_catalogue_refresh_outcomes",
)
_SUPPORTED_REVISIONS = frozenset((*_PRIOR_REVISIONS, _HEAD))
_JOURNAL = "phase6-journal-v3.json"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_TEMP_RE = re.compile(rf"\.phase6-journal-v3-({_UUID})-([1-9][0-9]*)\.tmp")

_PHASE_ORDER = {
    "EXISTING": (
        "PREPARING", "SOURCE_QUIESCED", "BACKUP_VERIFIED", "MIGRATING_COPY",
        "CANDIDATE_VALIDATED", "SWITCH_INTENT", "PROMOTED", "COMMITTED",
        "RESTORE_INTENT", "ROLLED_BACK",
    ),
    "ABSENT": (
        "ABSENT", "MIGRATING_COPY", "CANDIDATE_VALIDATED", "SWITCH_INTENT",
        "PROMOTED", "COMMITTED",
    ),
}
_PHASE_PRIOR = {
    "EXISTING": {
        "PREPARING": None,
        "SOURCE_QUIESCED": "PREPARING",
        "BACKUP_VERIFIED": "SOURCE_QUIESCED",
        "MIGRATING_COPY": "BACKUP_VERIFIED",
        "CANDIDATE_VALIDATED": "MIGRATING_COPY",
        "SWITCH_INTENT": "CANDIDATE_VALIDATED",
        "PROMOTED": "SWITCH_INTENT",
        "COMMITTED": "PROMOTED",
        "RESTORE_INTENT": "PROMOTED",
        "ROLLED_BACK": "RESTORE_INTENT",
    },
    "ABSENT": {
        "ABSENT": None,
        "MIGRATING_COPY": "ABSENT",
        "CANDIDATE_VALIDATED": "MIGRATING_COPY",
        "SWITCH_INTENT": "CANDIDATE_VALIDATED",
        "PROMOTED": "SWITCH_INTENT",
        "COMMITTED": "PROMOTED",
    },
}
_PHASE_SEQUENCE = {
    "EXISTING": {
        "PREPARING": 1,
        "SOURCE_QUIESCED": 2,
        "BACKUP_VERIFIED": 3,
        "MIGRATING_COPY": 4,
        "CANDIDATE_VALIDATED": 5,
        "SWITCH_INTENT": 6,
        "PROMOTED": 7,
        "COMMITTED": 8,
        "RESTORE_INTENT": 8,
        "ROLLED_BACK": 9,
    },
    "ABSENT": {
        "ABSENT": 1,
        "MIGRATING_COPY": 2,
        "CANDIDATE_VALIDATED": 3,
        "SWITCH_INTENT": 4,
        "PROMOTED": 5,
        "COMMITTED": 6,
    },
}
_COMMON_FIELDS = {
    "journal_version", "transaction_id", "sequence", "phase", "prior_phase",
    "target_revision", "source_kind", "canonical_leaf", "candidate_leaf",
    "candidate_rollback_journal_leaf", "next_update_leaf", "root_identity",
    "database_directory_identity", "migration_directory_identity",
    "recovery_directory_identity",
}


class _PromotedValidationError(RuntimeError):
    def __init__(self, failure_class: str):
        super().__init__("promoted database failed normal rooted-VFS validation")
        self.failure_class = failure_class


_TEST_FAULT_HOOK = None


def _fault(point: str) -> None:
    hook = _TEST_FAULT_HOOK
    if hook is not None:
        hook(point)


def _identity_record(value: FileIdentity) -> dict[str, object]:
    return {
        "device_major": value.device_major,
        "device_minor": value.device_minor,
        "inode": value.inode,
        "mount_id": value.mount_id,
        "type": "directory" if stat.S_ISDIR(value.mode) else "regular",
        "uid": value.uid,
        "mode": stat.S_IMODE(value.mode),
        "nlink": value.nlink,
    }


def _same_identity(record: object, value: FileIdentity) -> bool:
    return record == _identity_record(value)


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise RuntimeError("short write to migration artifact")
        offset += written


def _read_all(fd: int, *, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise RuntimeError("migration artifact exceeds its closed size limit")
        chunks.append(chunk)


def _own_fd(authority: DataRootAuthority, label: str, fd: int):
    """Put every provisional migration descriptor in the authority ledger."""
    return authority._claim_scoped_fd(f"migration:{label}", fd)


def _release_fd(authority: DataRootAuthority, fd: int) -> None:
    claim = next(
        (
            item
            for item in authority._claims
            if item.fd == fd
            and item.status == "HELD"
            and item.label.startswith("scoped:migration:")
        ),
        None,
    )
    if claim is None:
        raise RuntimeError("migration descriptor has no authority owner")
    authority._release_scoped_fd(claim)


def _safe_regular(
    authority: DataRootAuthority, directory_fd: int, leaf: str
) -> tuple[int, FileIdentity]:
    try:
        fd = os.open(
            leaf,
            os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise RuntimeError(f"migration artifact is unavailable: {leaf}") from exc
    claim = _own_fd(authority, f"regular:{leaf}", fd)
    try:
        value = _check_regular(fd, mount_id=authority._root_identity.mount_id)
        return fd, value
    except BaseException:
        authority._release_scoped_fd(claim)
        raise


def _leaf_identity(
    authority: DataRootAuthority, directory_fd: int, leaf: str
) -> FileIdentity | None:
    try:
        fd = os.open(
            leaf,
            os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"migration artifact is unsafe: {leaf}") from exc
    claim = _own_fd(authority, f"identity:{leaf}", fd)
    try:
        return _check_regular(fd, mount_id=authority._root_identity.mount_id)
    finally:
        authority._release_scoped_fd(claim)


def _hash_fd(fd: int) -> tuple[FileIdentity, str]:
    before = _identity(fd)
    digest = hashlib.sha256()
    offset = 0
    while offset < before.size:
        chunk = os.pread(fd, min(1024 * 1024, before.size - offset), offset)
        if not chunk:
            raise RuntimeError("database changed while being hashed")
        digest.update(chunk)
        offset += len(chunk)
    after = _identity(fd)
    if before != after:
        raise RuntimeError("database identity changed while being hashed")
    return after, digest.hexdigest()


def _hash_leaf(
    authority: DataRootAuthority, directory_fd: int, leaf: str
) -> tuple[FileIdentity, str]:
    fd, _ = _safe_regular(authority, directory_fd, leaf)
    try:
        return _hash_fd(fd)
    finally:
        _release_fd(authority, fd)


def _recorded_file_matches(
    authority: DataRootAuthority,
    directory_fd: int,
    leaf: str,
    *,
    identity: object,
    size: object,
    sha256: object,
) -> bool:
    value = _leaf_identity(authority, directory_fd, leaf)
    if value is None or not _same_identity(identity, value) or value.size != size:
        return False
    checked, digest = _hash_leaf(authority, directory_fd, leaf)
    return checked.size == size and digest == sha256


def _source_matches(
    authority: DataRootAuthority,
    directory_fd: int,
    leaf: str,
    record: dict[str, object],
) -> bool:
    return _recorded_file_matches(
        authority,
        directory_fd,
        leaf,
        identity=record["source_identity"],
        size=record["source_size"],
        sha256=record["source_sha256"],
    )


def _canonical_bytes(record: dict[str, object]) -> bytes:
    return (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def _uuid7_text(value: object) -> bool:
    if type(value) is not str or re.fullmatch(_UUID, value) is None:
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 7 and str(parsed) == value


def _valid_sha256(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_identity_record(value: object, *, expected_type: str) -> None:
    keys = {
        "device_major", "device_minor", "inode", "mount_id", "type", "uid",
        "mode", "nlink",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise RuntimeError("migration journal identity record is malformed")
    if value.get("type") != expected_type:
        raise RuntimeError("migration journal identity type is malformed")
    for key in keys - {"type"}:
        item = value.get(key)
        if type(item) is not int or item < 0:
            raise RuntimeError("migration journal identity value is malformed")
    expected_mode = 0o700 if expected_type == "directory" else 0o600
    if value["uid"] != os.geteuid() or value["mode"] != expected_mode:
        raise RuntimeError("migration journal identity policy is malformed")
    if expected_type == "regular" and value["nlink"] != 1:
        raise RuntimeError("migration journal regular identity has unsafe links")


def _phase_fields(source_kind: str, phase: str) -> set[str]:
    fields = set(_COMMON_FIELDS)
    if source_kind == "EXISTING":
        fields |= {
            "expected_start_revision", "source_identity", "backup_leaf",
            "backup_temp_leaf",
        }
        if phase != "PREPARING":
            fields |= {
                "recovered_revision", "source_size", "source_sha256",
                "source_quiesced",
            }
        if phase not in {"PREPARING", "SOURCE_QUIESCED"}:
            fields |= {
                "backup_identity", "backup_size", "backup_sha256",
                "backup_verified",
            }
    if phase in {
        "MIGRATING_COPY", "CANDIDATE_VALIDATED", "SWITCH_INTENT", "PROMOTED",
        "COMMITTED", "RESTORE_INTENT", "ROLLED_BACK",
    }:
        fields |= {"seed_kind", "seed_size"}
    if phase in {
        "CANDIDATE_VALIDATED", "SWITCH_INTENT", "PROMOTED", "COMMITTED",
        "RESTORE_INTENT", "ROLLED_BACK",
    }:
        fields |= {
            "candidate_identity", "candidate_size", "candidate_sha256",
            "candidate_revision", "candidate_quiesced", "validation",
        }
    if phase in {
        "SWITCH_INTENT", "PROMOTED", "COMMITTED", "RESTORE_INTENT",
        "ROLLED_BACK",
    }:
        fields |= {"publication_primitive"}
    if phase in {"PROMOTED", "COMMITTED", "RESTORE_INTENT", "ROLLED_BACK"}:
        fields |= {"promoted"}
    if phase in {"RESTORE_INTENT", "ROLLED_BACK"}:
        fields |= {"restore_primitive", "validation_failure_class"}
    if phase == "ROLLED_BACK":
        fields |= {"restored"}
    if phase in {"COMMITTED", "ROLLED_BACK"}:
        fields |= {"normal_vfs_validated", "canonical_identity"}
    return fields


def _parse_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError("migration journal contains duplicate fields")
        result[key] = value
    return result


def _validate_record(
    authority: DataRootAuthority, record: object, raw: bytes
) -> dict[str, object]:
    if not isinstance(record, dict):
        raise RuntimeError("migration journal is not an object")
    if record.get("journal_version") != 3 or record.get("target_revision") != _HEAD:
        raise RuntimeError("migration journal version or target is unsupported")
    source_kind = record.get("source_kind")
    phase = record.get("phase")
    if source_kind not in _PHASE_ORDER or phase not in _PHASE_ORDER[str(source_kind)]:
        raise RuntimeError("migration journal phase graph is invalid")
    sequence = _PHASE_SEQUENCE[str(source_kind)][str(phase)]
    if type(record.get("sequence")) is not int or record["sequence"] != sequence:
        raise RuntimeError("migration journal sequence is invalid")
    expected_prior = _PHASE_PRIOR[str(source_kind)][str(phase)]
    if record.get("prior_phase") != expected_prior:
        raise RuntimeError("migration journal prior phase is invalid")
    transaction_id = record.get("transaction_id")
    if not _uuid7_text(transaction_id):
        raise RuntimeError("migration journal transaction identity is invalid")
    candidate = f"migrate-{transaction_id}.candidate.sqlite3"
    if (
        record.get("canonical_leaf") != "state.sqlite3"
        or record.get("candidate_leaf") != candidate
        or record.get("candidate_rollback_journal_leaf") != candidate + "-journal"
    ):
        raise RuntimeError("migration journal database leaves are invalid")
    expected_temp = f".phase6-journal-v3-{transaction_id}-{sequence + 1}.tmp"
    if record.get("next_update_leaf") != expected_temp:
        raise RuntimeError("migration journal update leaf is invalid")
    if set(record) != _phase_fields(str(source_kind), str(phase)):
        raise RuntimeError("migration journal fields are not the closed phase schema")
    if raw != _canonical_bytes(record):
        raise RuntimeError("migration journal is not canonical JSON")
    if source_kind == "EXISTING":
        if record.get("expected_start_revision") not in _PRIOR_REVISIONS:
            raise RuntimeError("migration journal source revision is unsupported")
        if record.get("backup_leaf") != f"migrate-{transaction_id}.backup.sqlite3":
            raise RuntimeError("migration journal backup leaf is invalid")
        if record.get("backup_temp_leaf") != f"migrate-{transaction_id}.backup.tmp":
            raise RuntimeError("migration journal backup temporary leaf is invalid")
        _validate_identity_record(record.get("source_identity"), expected_type="regular")
    for name in (
        "root_identity", "database_directory_identity",
        "migration_directory_identity", "recovery_directory_identity",
    ):
        _validate_identity_record(record.get(name), expected_type="directory")
    if phase != "PREPARING" and source_kind == "EXISTING":
        if (
            record.get("recovered_revision") != record.get("expected_start_revision")
            or type(record.get("source_size")) is not int
            or record["source_size"] < 1
            or not _valid_sha256(record.get("source_sha256"))
            or record.get("source_quiesced") is not True
        ):
            raise RuntimeError("migration journal source facts are malformed")
    if source_kind == "EXISTING" and phase not in {"PREPARING", "SOURCE_QUIESCED"}:
        _validate_identity_record(record.get("backup_identity"), expected_type="regular")
        if (
            record.get("backup_size") != record.get("source_size")
            or record.get("backup_sha256") != record.get("source_sha256")
            or record.get("backup_verified") is not True
        ):
            raise RuntimeError("migration journal backup facts are malformed")
    if phase in {
        "MIGRATING_COPY", "CANDIDATE_VALIDATED", "SWITCH_INTENT", "PROMOTED",
        "COMMITTED", "RESTORE_INTENT", "ROLLED_BACK",
    }:
        expected_seed = "VERIFIED_BACKUP" if source_kind == "EXISTING" else "EMPTY_SQLITE"
        if (
            record.get("seed_kind") != expected_seed
            or type(record.get("seed_size")) is not int
            or record["seed_size"] < 0
            or (source_kind == "ABSENT" and record["seed_size"] != 0)
            or (
                source_kind == "EXISTING"
                and record["seed_size"] != record.get("backup_size")
            )
        ):
            raise RuntimeError("migration journal candidate seed facts are malformed")
    if phase in {
        "CANDIDATE_VALIDATED", "SWITCH_INTENT", "PROMOTED", "COMMITTED",
        "RESTORE_INTENT", "ROLLED_BACK",
    }:
        _validate_identity_record(record.get("candidate_identity"), expected_type="regular")
        if (
            type(record.get("candidate_size")) is not int
            or record["candidate_size"] < 1
            or not _valid_sha256(record.get("candidate_sha256"))
            or record.get("candidate_revision") != _HEAD
            or record.get("candidate_quiesced") is not True
            or record.get("validation")
            != "integrity-schema-behavior-rows-attachments"
        ):
            raise RuntimeError("migration journal candidate facts are malformed")
    if phase in {
        "SWITCH_INTENT", "PROMOTED", "COMMITTED", "RESTORE_INTENT",
        "ROLLED_BACK",
    }:
        expected_primitive = (
            "RENAME_EXCHANGE" if source_kind == "EXISTING" else "RENAME_NOREPLACE"
        )
        if record.get("publication_primitive") != expected_primitive:
            raise RuntimeError("migration journal publication primitive is malformed")
    if phase in {"PROMOTED", "COMMITTED", "RESTORE_INTENT", "ROLLED_BACK"}:
        if record.get("promoted") is not True:
            raise RuntimeError("migration journal promotion fact is malformed")
    if phase in {"RESTORE_INTENT", "ROLLED_BACK"}:
        if (
            source_kind != "EXISTING"
            or record.get("restore_primitive") != "RENAME_EXCHANGE"
            or record.get("validation_failure_class")
            not in {"NORMAL_VFS_OPEN", "NORMAL_VFS_VALIDATION", "CLAIM_REPLACEMENT"}
        ):
            raise RuntimeError("migration journal restore facts are malformed")
    if phase == "ROLLED_BACK" and record.get("restored") is not True:
        raise RuntimeError("migration journal restore result is malformed")
    if phase in {"COMMITTED", "ROLLED_BACK"}:
        _validate_identity_record(record.get("canonical_identity"), expected_type="regular")
        if record.get("normal_vfs_validated") is not True:
            raise RuntimeError("migration journal terminal validation is malformed")
    identities = {
        "root_identity": authority._root_identity,
        "database_directory_identity": _identity(authority._database_dir_capability),
        "migration_directory_identity": _identity(
            authority._directory_fd("database/migration")
        ),
        "recovery_directory_identity": _identity(authority._directory_fd("recovery")),
    }
    for name, value in identities.items():
        if not _same_identity(record.get(name), value):
            raise RuntimeError(f"migration journal {name} no longer matches authority")
    return record


def _read_journal(authority: DataRootAuthority) -> dict[str, object] | None:
    migration_fd = authority._directory_fd("database/migration")
    try:
        fd = os.open(
            _JOURNAL,
            os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=migration_fd,
        )
    except FileNotFoundError:
        return None
    claim = _own_fd(authority, "journal-read", fd)
    try:
        value = _check_regular(fd, mount_id=authority._root_identity.mount_id)
        if value.size > 128 * 1024:
            raise RuntimeError("migration journal is too large")
        raw = _read_all(fd, limit=128 * 1024)
    finally:
        authority._release_scoped_fd(claim)
    try:
        record = json.loads(raw, object_pairs_hook=_parse_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("migration journal is malformed") from exc
    return _validate_record(authority, record, raw)


def _journal_base(
    authority: DataRootAuthority, transaction_id: str, source_kind: str
) -> dict[str, object]:
    candidate = f"migrate-{transaction_id}.candidate.sqlite3"
    return {
        "journal_version": 3,
        "transaction_id": transaction_id,
        "target_revision": _HEAD,
        "source_kind": source_kind,
        "canonical_leaf": "state.sqlite3",
        "candidate_leaf": candidate,
        "candidate_rollback_journal_leaf": candidate + "-journal",
        "root_identity": _identity_record(authority._root_identity),
        "database_directory_identity": _identity_record(
            _identity(authority._database_dir_capability)
        ),
        "migration_directory_identity": _identity_record(
            _identity(authority._directory_fd("database/migration"))
        ),
        "recovery_directory_identity": _identity_record(
            _identity(authority._directory_fd("recovery"))
        ),
    }


def _advance(
    record: dict[str, object], phase: str, **facts: object
) -> dict[str, object]:
    source_kind = str(record["source_kind"])
    if phase not in _PHASE_PRIOR[source_kind]:
        raise RuntimeError("internal migration phase is invalid")
    prior = _PHASE_PRIOR[source_kind][phase]
    if prior != record.get("phase") and not (
        prior is None and record.get("phase") is None
    ):
        raise RuntimeError("internal migration phase transition is invalid")
    sequence = _PHASE_SEQUENCE[source_kind][phase]
    allowed = _phase_fields(str(record["source_kind"]), phase)
    next_record = {key: value for key, value in record.items() if key in allowed}
    next_record.update(facts)
    next_record.update(
        phase=phase,
        prior_phase=prior,
        sequence=sequence,
        next_update_leaf=(
            f".phase6-journal-v3-{record['transaction_id']}-{sequence + 1}.tmp"
        ),
    )
    if set(next_record) != allowed:
        missing = sorted(allowed - set(next_record))
        extra = sorted(set(next_record) - allowed)
        raise RuntimeError(
            f"internal migration journal mismatch: missing={missing}, extra={extra}"
        )
    return next_record


def _write_journal(
    authority: DataRootAuthority,
    record: dict[str, object],
    *,
    initial: bool = False,
) -> None:
    migration_fd = authority._directory_fd("database/migration")
    _validate_record(authority, record, _canonical_bytes(record))
    _fault(f"before-journal-{record['phase']}")
    leaf = (
        f".phase6-journal-v3-{record['transaction_id']}-{record['sequence']}.tmp"
    )
    fd = os.open(
        leaf,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=migration_fd,
    )
    claim = _own_fd(authority, "journal-write", fd)
    try:
        _fault(f"after-journal-create-{record['phase']}")
        _write_all(fd, _canonical_bytes(record))
        _fault(f"after-journal-write-{record['phase']}")
        os.fsync(fd)
        _fault(f"after-journal-file-fsync-{record['phase']}")
    finally:
        authority._release_scoped_fd(claim)
    if initial:
        from bots5.infrastructure.attachments import _rename_noreplace

        _fault(f"before-journal-rename-{record['phase']}")
        _rename_noreplace(migration_fd, leaf, migration_fd, _JOURNAL)
    else:
        _fault(f"before-journal-rename-{record['phase']}")
        os.rename(leaf, _JOURNAL, src_dir_fd=migration_fd, dst_dir_fd=migration_fd)
    _fault(f"after-journal-rename-{record['phase']}")
    os.fsync(migration_fd)
    _fault(f"after-journal-directory-fsync-{record['phase']}")


def _remove_leaf(
    authority: DataRootAuthority, directory_fd: int, leaf: str
) -> None:
    if _leaf_identity(authority, directory_fd, leaf) is None:
        return
    fd, opened_identity = _safe_regular(authority, directory_fd, leaf)
    try:
        named = _leaf_identity(authority, directory_fd, leaf)
        if named is None or named.key != opened_identity.key:
            raise RuntimeError("migration artifact changed before deletion")
        os.unlink(leaf, dir_fd=directory_fd)
    finally:
        _release_fd(authority, fd)
    _fault(f"after-unlink-{leaf}")
    os.fsync(directory_fd)
    _fault(f"after-unlink-directory-fsync-{leaf}")


def _clean_initial_temp(authority: DataRootAuthority) -> None:
    migration_fd = authority._directory_fd("database/migration")
    names = authority.fresh_directory_inventory("database/migration")
    if names:
        if (
            len(names) != 1
            or _TEMP_RE.fullmatch(names[0]) is None
            or not names[0].endswith("-1.tmp")
        ):
            raise RuntimeError("unattributed migration artifact exists without a journal")
        fd, _ = _safe_regular(authority, migration_fd, names[0])
        _release_fd(authority, fd)
        _remove_leaf(authority, migration_fd, names[0])
    if authority.fresh_directory_inventory("recovery"):
        raise RuntimeError("unattributed recovery artifact exists without a journal")


def _clean_next_temp(
    authority: DataRootAuthority, record: dict[str, object]
) -> None:
    migration_fd = authority._directory_fd("database/migration")
    leaf = str(record["next_update_leaf"])
    if leaf in authority.fresh_directory_inventory("database/migration"):
        fd, _ = _safe_regular(authority, migration_fd, leaf)
        _release_fd(authority, fd)
        _remove_leaf(authority, migration_fd, leaf)


def _vfs_for(
    authority: DataRootAuthority,
    directory_fd: int,
    claim_fd: int,
    leaf: str,
    *,
    intake_wal: bool = False,
) -> RootedSQLiteVfs:
    try:
        return RootedSQLiteVfs(
            database_dir_fd=directory_fd,
            main_claim_fd=claim_fd,
            temp_dir_fd=authority._directory_fd("database/temp"),
            mount_id=authority._root_identity.mount_id,
            main_leaf=leaf,
            journal_leaf=leaf + "-journal",
            intake_wal=intake_wal,
            authority=authority,
            resource_label=f"migration-vfs:{leaf}:{uuid.uuid4().hex}",
        )
    except RootedVfsRegistrationCloseUnknown:
        raise AuthorityError(
            "rooted VFS registration cleanup incomplete"
        ) from None


def _revision(vfs: RootedSQLiteVfs) -> str | None:
    connection = None
    try:
        connection = vfs.connect()
        rows = connection.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("cannot inspect database schema through rooted VFS") from exc
    finally:
        if connection is not None:
            connection.close()
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeError("database has an invalid Alembic revision set")
    return str(rows[0][0])


def _open_intake(authority: DataRootAuthority) -> RootedSQLiteVfs:
    assert authority._main_claim is not None and authority._main_claim.fd is not None
    return _vfs_for(
        authority,
        authority._database_dir_capability,
        authority._main_claim.fd,
        "state.sqlite3",
        intake_wal=True,
    )


def _prepare_source_sidecars(authority: DataRootAuthority) -> None:
    """Validate and parent-sync visible fixed sidecars before SQLite recovery."""
    database_fd = authority._database_dir_capability
    names = set(authority.fresh_directory_inventory("database"))
    for leaf in (
        "state.sqlite3-journal",
        "state.sqlite3-wal",
        "state.sqlite3-shm",
    ):
        if leaf not in names:
            continue
        fd, _ = _safe_regular(authority, database_fd, leaf)
        try:
            pass
        finally:
            _release_fd(authority, fd)
    os.fsync(database_fd)
    _fault("after-source-sidecar-parent-fsync")


def _discover_existing(authority: DataRootAuthority) -> str:
    _prepare_source_sidecars(authority)
    vfs = _open_intake(authority)
    try:
        revision = _revision(vfs)
        if revision in _SUPPORTED_REVISIONS and revision not in {
            "0001_desktop_state",
            "0002_conversation_lineage",
            "0003_integrity_boundaries",
            "0004_integrity_guard_function",
        }:
            connection = vfs.connect()
            try:
                column_info = {
                    str(row[1]): row
                    for row in connection.execute(
                        "PRAGMA table_info(generation_attempts)"
                    ).fetchall()
                }
            finally:
                connection.close()
            expected = {
                "provider_id": "VARCHAR(64)",
                "returned_model": "TEXT",
                "request_id": "TEXT",
                "finish_reason": "VARCHAR(128)",
                "prompt_tokens": "INTEGER",
                "completion_tokens": "INTEGER",
                "reasoning_tokens": "INTEGER",
                "total_tokens": "INTEGER",
                "known_cost_usd": "TEXT",
                "remote_outcome_unknown": "BOOLEAN",
            }
            missing = sorted(set(expected) - set(column_info))
            if missing:
                raise RuntimeError(
                    "current Phase 3 schema is missing generation outcome columns: "
                    + ", ".join(missing)
                )
            non_nullable = sorted(
                name for name in expected if int(column_info[name][3]) != 0
            )
            if non_nullable:
                raise RuntimeError(
                    "current Phase 3 schema outcome columns must be nullable: "
                    + ", ".join(non_nullable)
                )
            wrong_types = sorted(
                name
                for name, expected_type in expected.items()
                if str(column_info[name][2]).upper().replace(" ", "")
                != expected_type
            )
            if wrong_types:
                raise RuntimeError(
                    "current Phase 3 schema outcome columns have invalid declared types: "
                    + ", ".join(
                        f"{name}={column_info[name][2]}" for name in wrong_types
                    )
                )
    finally:
        if vfs.open_count != 0:
            raise RuntimeError("migration discovery left SQLite handles open")
        vfs.close()
    if revision not in _SUPPORTED_REVISIONS:
        raise RuntimeError(
            "database schema revision is absent, corrupt, newer or unsupported"
        )
    return str(revision)


def _quiesce_source(
    authority: DataRootAuthority, expected_revision: str
) -> tuple[FileIdentity, str]:
    _prepare_source_sidecars(authority)
    vfs = _open_intake(authority)
    try:
        _fault("before-source-recovery")
        connection = vfs.connect()
        try:
            initial_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            _fault("after-source-recovery")
            connection.execute("PRAGMA synchronous=FULL")
            synchronous = int(
                connection.execute("PRAGMA synchronous").fetchone()[0]
            )
            if synchronous != 2:
                raise RuntimeError("migration intake is not synchronous FULL")
            if initial_mode == "wal":
                mode = str(
                    connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                ).lower()
                if mode != "delete":
                    raise RuntimeError("migration intake WAL checkpoint did not drain")
                _fault("after-source-wal-checkpoint")
            elif initial_mode != "delete":
                raise RuntimeError("migration source journal mode is unsupported")
            else:
                mode = str(
                    connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                ).lower()
            if mode != "delete":
                raise RuntimeError("migration intake could not enter DELETE journal mode")
            if int(connection.execute("PRAGMA synchronous").fetchone()[0]) != 2:
                raise RuntimeError("migration intake lost synchronous FULL")
            _fault("after-source-delete-mode")
            rows = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchall()
            if rows != [(expected_revision,)]:
                raise RuntimeError("migration source revision changed during quiescence")
        finally:
            connection.close()
    finally:
        if vfs.open_count != 0:
            raise RuntimeError("migration intake left SQLite handles open")
        vfs.close()
    database_fd = authority._database_dir_capability
    for leaf in ("state.sqlite3-journal", "state.sqlite3-wal", "state.sqlite3-shm"):
        identity = _leaf_identity(authority, database_fd, leaf)
        if identity is not None:
            # After the exclusive intake connection has recovered/checkpointed,
            # entered DELETE mode, closed, and drained the VFS, any remaining
            # fixed sidecar is non-live residue (for example a pre-commit
            # rollback journal with a zeroed header).  Remove only that exact
            # safe authority-owned leaf, then durably prove sidecar absence.
            _remove_leaf(authority, database_fd, leaf)
    assert authority._main_claim is not None and authority._main_claim.fd is not None
    os.fsync(authority._main_claim.fd)
    _fault("after-source-file-fsync")
    os.fsync(database_fd)
    _fault("after-source-directory-fsync")
    return _hash_fd(authority._main_claim.fd)


def _copy_fd(source_fd: int, destination_fd: int) -> None:
    offset = 0
    while True:
        chunk = os.pread(source_fd, 1024 * 1024, offset)
        if not chunk:
            break
        _write_all(destination_fd, chunk)
        offset += len(chunk)
    os.fsync(destination_fd)


def _verify_database(
    vfs: RootedSQLiteVfs,
    revision: str,
    *,
    phase6: bool = False,
    destructive_phase6: bool = False,
) -> None:
    engine = create_engine(
        "sqlite://", creator=vfs.connect, poolclass=NullPool, future=True
    )
    event.listen(engine, "connect", install_transition_guard)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA synchronous=FULL")
            synchronous = int(
                connection.exec_driver_sql("PRAGMA synchronous").scalar_one()
            )
            mode = str(
                connection.exec_driver_sql("PRAGMA journal_mode=DELETE").scalar_one()
            ).lower()
            if mode != "delete" or synchronous != 2:
                raise RuntimeError(
                    "migration validation requires DELETE/FULL before writes"
                )
            result = connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
            if result != "ok":
                raise RuntimeError("SQLite integrity check failed")
            actual = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
            if actual != revision:
                raise RuntimeError("database revision does not match migration phase")
            if phase6:
                from bots5.infrastructure.persistence.sqlite import (
                    _validate_open_connection,
                )

                _validate_open_connection(
                    connection,
                    expected_revision=_HEAD,
                    destructive_phase6=destructive_phase6,
                )
    finally:
        engine.dispose()
    if vfs.open_count != 0:
        raise RuntimeError("database validation left SQLite handles open")


def _make_backup(
    authority: DataRootAuthority, record: dict[str, object]
) -> dict[str, object]:
    assert authority._main_claim is not None and authority._main_claim.fd is not None
    source_identity, source_hash = _hash_fd(authority._main_claim.fd)
    if (
        source_hash != record["source_sha256"]
        or source_identity.size != record["source_size"]
    ):
        raise RuntimeError("quiescent source changed before backup")
    recovery_fd = authority._directory_fd("recovery")
    backup = str(record["backup_leaf"])
    temporary = str(record["backup_temp_leaf"])
    existing = _leaf_identity(authority, recovery_fd, backup)
    if existing is None:
        if _leaf_identity(authority, recovery_fd, temporary) is not None:
            _remove_leaf(authority, recovery_fd, temporary)
        temp_fd = os.open(
            temporary,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=recovery_fd,
        )
        temp_claim = _own_fd(authority, "backup-temp", temp_fd)
        try:
            _fault("after-backup-temp-create")
            _copy_fd(authority._main_claim.fd, temp_fd)
            _fault("after-backup-temp-file-fsync")
        finally:
            authority._release_scoped_fd(temp_claim)
        from bots5.infrastructure.attachments import _rename_noreplace

        _rename_noreplace(recovery_fd, temporary, recovery_fd, backup)
        _fault("after-backup-rename")
        os.fsync(recovery_fd)
        _fault("after-backup-directory-fsync")
    backup_fd, backup_identity = _safe_regular(authority, recovery_fd, backup)
    try:
        checked, backup_hash = _hash_fd(backup_fd)
        vfs = _vfs_for(authority, recovery_fd, backup_fd, backup)
        try:
            _verify_database(vfs, str(record["expected_start_revision"]))
        finally:
            vfs.close()
    finally:
        _release_fd(authority, backup_fd)
    if backup_hash != source_hash or checked.size != source_identity.size:
        raise RuntimeError("verified migration backup differs from quiescent source")
    return _advance(
        record,
        "BACKUP_VERIFIED",
        backup_identity=_identity_record(backup_identity),
        backup_size=checked.size,
        backup_sha256=backup_hash,
        backup_verified=True,
    )


def _verify_source_and_backup(
    authority: DataRootAuthority, record: dict[str, object]
) -> None:
    if not _source_matches(
        authority,
        authority._database_dir_capability,
        "state.sqlite3",
        record,
    ):
        raise RuntimeError("quiescent migration source changed before candidate rebuild")
    for leaf in ("state.sqlite3-journal", "state.sqlite3-wal", "state.sqlite3-shm"):
        if _leaf_identity(authority, authority._database_dir_capability, leaf) is not None:
            raise RuntimeError("quiescent migration source regained a SQLite sidecar")
    recovery_fd = authority._directory_fd("recovery")
    if not _recorded_file_matches(
        authority,
        recovery_fd,
        str(record["backup_leaf"]),
        identity=record["backup_identity"],
        size=record["backup_size"],
        sha256=record["backup_sha256"],
    ):
        raise RuntimeError("verified migration backup changed before candidate rebuild")
    backup_fd, _ = _safe_regular(authority, recovery_fd, str(record["backup_leaf"]))
    try:
        vfs = _vfs_for(
            authority, recovery_fd, backup_fd, str(record["backup_leaf"])
        )
        try:
            _verify_database(vfs, str(record["expected_start_revision"]))
        finally:
            vfs.close()
    finally:
        _release_fd(authority, backup_fd)


def _preflight_bundle(
    authority: DataRootAuthority, record: dict[str, object]
) -> None:
    migration_fd = authority._directory_fd("database/migration")
    allowed = {
        _JOURNAL,
        str(record["candidate_leaf"]),
        str(record["candidate_rollback_journal_leaf"]),
    }
    next_temp = str(record["next_update_leaf"])
    migration_names = set(
        authority.fresh_directory_inventory("database/migration")
    )
    if next_temp in migration_names:
        allowed.add(next_temp)
    unexpected = sorted(migration_names - allowed)
    if unexpected:
        raise RuntimeError(
            "migration directory contains unattributed evidence: "
            + ", ".join(unexpected)
        )
    for leaf in (
        str(record["candidate_leaf"]),
        str(record["candidate_rollback_journal_leaf"]),
    ):
        _leaf_identity(authority, migration_fd, leaf)
    if record["source_kind"] == "EXISTING":
        recovery_fd = authority._directory_fd("recovery")
        allowed_recovery = {
            str(record["backup_leaf"]), str(record["backup_temp_leaf"])
        }
        unexpected_recovery = sorted(
            set(authority.fresh_directory_inventory("recovery"))
            - allowed_recovery
        )
        if unexpected_recovery:
            raise RuntimeError(
                "recovery directory contains unattributed evidence: "
                + ", ".join(unexpected_recovery)
            )
        for leaf in allowed_recovery:
            _leaf_identity(authority, recovery_fd, leaf)
    elif authority.fresh_directory_inventory("recovery"):
        raise RuntimeError("fresh migration has unattributed recovery evidence")


def _dispose_bundle(
    authority: DataRootAuthority, record: dict[str, object]
) -> None:
    authority._release_migration_candidate()
    _preflight_bundle(authority, record)
    migration_fd = authority._directory_fd("database/migration")
    _remove_leaf(
        authority, migration_fd, str(record["candidate_rollback_journal_leaf"])
    )
    _remove_leaf(authority, migration_fd, str(record["candidate_leaf"]))
    if any(
        _leaf_identity(authority, migration_fd, leaf) is not None
        for leaf in (
            str(record["candidate_leaf"]),
            str(record["candidate_rollback_journal_leaf"]),
        )
    ):
        raise RuntimeError("migration candidate bundle disposal is incomplete")
    _fault("after-candidate-bundle-absence-proof")
    os.fsync(migration_fd)
    _fault("after-candidate-bundle-directory-fsync")


def _candidate_seed(authority: DataRootAuthority, record: dict[str, object]) -> int:
    migration_fd = authority._directory_fd("database/migration")
    leaf = str(record["candidate_leaf"])
    candidate_fd = os.open(
        leaf,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=migration_fd,
    )
    candidate_claim = authority._claim_scoped_fd("migration-candidate-seed", candidate_fd)
    try:
        _fault("after-candidate-create")
        if record["source_kind"] == "EXISTING":
            _verify_source_and_backup(authority, record)
            recovery_fd = authority._directory_fd("recovery")
            backup_fd, _ = _safe_regular(
                authority, recovery_fd, str(record["backup_leaf"])
            )
            try:
                backup_identity, backup_hash = _hash_fd(backup_fd)
                if (
                    backup_identity.size != record["backup_size"]
                    or backup_hash != record["backup_sha256"]
                ):
                    raise RuntimeError("migration backup changed before candidate rebuild")
                _copy_fd(backup_fd, candidate_fd)
                _fault("after-candidate-seed-copy-fsync")
            finally:
                _release_fd(authority, backup_fd)
        else:
            if os.fstat(candidate_fd).st_size != 0:
                raise RuntimeError("fresh migration candidate seed is not empty")
            os.fsync(candidate_fd)
            _fault("after-empty-candidate-fsync")
        fcntl.flock(candidate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.fsync(migration_fd)
        _fault("after-candidate-seed-directory-fsync")
        return candidate_fd
    except BaseException:
        authority._release_scoped_fd(candidate_claim)
        raise


def _run_alembic(vfs: RootedSQLiteVfs) -> None:
    engine = create_engine(
        "sqlite://", creator=vfs.connect, poolclass=NullPool, future=True
    )
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).with_name("migrations"))
    )

    def after_revision(**kwargs) -> None:
        step = kwargs.get("step")
        revision = getattr(step, "up_revision_id", None)
        _fault(f"after-candidate-revision-{revision or 'unknown'}")

    config.attributes["on_version_apply"] = after_revision
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA synchronous=FULL")
            synchronous = int(
                connection.exec_driver_sql("PRAGMA synchronous").scalar_one()
            )
            mode = str(
                connection.exec_driver_sql("PRAGMA journal_mode=DELETE").scalar_one()
            ).lower()
            if mode != "delete" or synchronous != 2:
                raise RuntimeError(
                    "candidate migration requires DELETE/FULL before Alembic"
                )
            _fault("after-candidate-delete-full-config")
            config.attributes["connection"] = connection
            command.upgrade(config, _HEAD)
    finally:
        engine.dispose()
    if vfs.open_count != 0:
        raise RuntimeError("candidate migration left SQLite handles open")


def _build_candidate(
    authority: DataRootAuthority, record: dict[str, object]
) -> dict[str, object]:
    _dispose_bundle(authority, record)
    candidate_fd = _candidate_seed(authority, record)
    migration_fd = authority._directory_fd("database/migration")
    adopted = False
    try:
        vfs = _vfs_for(
            authority, migration_fd, candidate_fd, str(record["candidate_leaf"])
        )
        try:
            if record["phase"] != "MIGRATING_COPY":
                seed_kind = (
                    "VERIFIED_BACKUP"
                    if record["source_kind"] == "EXISTING"
                    else "EMPTY_SQLITE"
                )
                next_record = _advance(
                    record,
                    "MIGRATING_COPY",
                    seed_kind=seed_kind,
                    seed_size=os.fstat(candidate_fd).st_size,
                )
                _write_journal(authority, next_record)
                record = next_record
            _run_alembic(vfs)
            _fault("after-candidate-alembic")
            connection = vfs.connect()
            try:
                connection.execute("PRAGMA synchronous=FULL")
                synchronous = int(
                    connection.execute("PRAGMA synchronous").fetchone()[0]
                )
                mode = str(
                    connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                ).lower()
                if mode != "delete":
                    raise RuntimeError("candidate did not quiesce in DELETE mode")
                if synchronous != 2:
                    raise RuntimeError("candidate did not quiesce with synchronous FULL")
                _fault("after-candidate-delete-mode")
            finally:
                connection.close()
            _verify_database(
                vfs, _HEAD, phase6=True, destructive_phase6=True
            )
        finally:
            if vfs.open_count != 0:
                raise RuntimeError("candidate VFS did not drain")
            vfs.close()
        for suffix in ("-journal", "-wal", "-shm"):
            if (
                _leaf_identity(
                    authority, migration_fd, str(record["candidate_leaf"]) + suffix
                )
                is not None
            ):
                raise RuntimeError("validated candidate retains a SQLite sidecar")
        os.fsync(candidate_fd)
        _fault("after-candidate-file-fsync")
        os.fsync(migration_fd)
        _fault("after-candidate-directory-fsync")
        candidate_identity, candidate_hash = _hash_fd(candidate_fd)
        _fault("after-candidate-hash")
        # Ownership transfers to the adoption routine even if its validation
        # fails; the candidate descriptor is never closed by two owners.
        adopted = True
        authority._adopt_migration_candidate(
            candidate_fd, migration_fd, str(record["candidate_leaf"])
        )
        _fault("after-candidate-claim")
        return _advance(
            record,
            "CANDIDATE_VALIDATED",
            candidate_identity=_identity_record(candidate_identity),
            candidate_size=candidate_identity.size,
            candidate_sha256=candidate_hash,
            candidate_revision=_HEAD,
            candidate_quiesced=True,
            validation="integrity-schema-behavior-rows-attachments",
        )
    finally:
        if not adopted:
            claim = next(
                (
                    item
                    for item in authority._claims
                    if item.fd == candidate_fd
                    and item.status == "HELD"
                    and item.label.startswith("scoped:")
                ),
                None,
            )
            if claim is not None:
                authority._release_scoped_fd(claim)


def _candidate_matches(
    authority: DataRootAuthority,
    directory_fd: int,
    leaf: str,
    record: dict[str, object],
) -> bool:
    return _recorded_file_matches(
        authority,
        directory_fd,
        leaf,
        identity=record["candidate_identity"],
        size=record["candidate_size"],
        sha256=record["candidate_sha256"],
    )


def _require_no_sidecars(
    authority: DataRootAuthority, directory_fd: int, leaf: str
) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        if _leaf_identity(authority, directory_fd, leaf + suffix) is not None:
            raise RuntimeError("consequential migration database has a SQLite sidecar")


def _validate_private_candidate(
    authority: DataRootAuthority, record: dict[str, object]
) -> None:
    migration_fd = authority._directory_fd("database/migration")
    leaf = str(record["candidate_leaf"])
    if not _candidate_matches(authority, migration_fd, leaf, record):
        raise RuntimeError("validated migration candidate changed")
    _require_no_sidecars(authority, migration_fd, leaf)
    if authority._migration_claim is None:
        candidate_fd, _ = _safe_regular(authority, migration_fd, leaf)
        authority._adopt_migration_candidate(candidate_fd, migration_fd, leaf)
    claim = authority._migration_claim
    if claim is None or claim.fd is None:
        raise RuntimeError("validated candidate claim is unavailable")
    if (
        authority._migration_identity is None
        or not _same_identity(record["candidate_identity"], authority._migration_identity)
    ):
        raise RuntimeError("validated candidate claim has the wrong identity")


def _publish(
    authority: DataRootAuthority, record: dict[str, object]
) -> dict[str, object]:
    database_fd = authority._database_dir_capability
    migration_fd = authority._directory_fd("database/migration")
    candidate = str(record["candidate_leaf"])
    if record["phase"] == "CANDIDATE_VALIDATED":
        _validate_private_candidate(authority, record)
        record = _advance(
            record,
            "SWITCH_INTENT",
            publication_primitive=(
                "RENAME_EXCHANGE"
                if record["source_kind"] == "EXISTING"
                else "RENAME_NOREPLACE"
            ),
        )
        _write_journal(authority, record)
    candidate_at_private = _candidate_matches(
        authority, migration_fd, candidate, record
    )
    candidate_at_canonical = _candidate_matches(
        authority, database_fd, "state.sqlite3", record
    )
    if candidate_at_private:
        _require_no_sidecars(authority, migration_fd, candidate)
    if candidate_at_canonical:
        _require_no_sidecars(authority, database_fd, "state.sqlite3")
    if record["source_kind"] == "ABSENT":
        if (
            candidate_at_private
            and _leaf_identity(authority, database_fd, "state.sqlite3") is None
        ):
            from bots5.infrastructure.attachments import _rename_noreplace

            _rename_noreplace(
                migration_fd, candidate, database_fd, "state.sqlite3"
            )
            _fault("after-publication-rename-noreplace")
        elif (
            not candidate_at_canonical
            or _leaf_identity(authority, migration_fd, candidate) is not None
        ):
            raise RuntimeError("fresh migration publication position is ambiguous")
    else:
        source_at_canonical = _source_matches(
            authority, database_fd, "state.sqlite3", record
        )
        private_identity = _leaf_identity(authority, migration_fd, candidate)
        source_at_private = (
            private_identity is not None
            and _source_matches(authority, migration_fd, candidate, record)
        )
        if candidate_at_private and source_at_canonical:
            from bots5.infrastructure.attachments import _rename_exchange

            _rename_exchange(
                migration_fd, candidate, database_fd, "state.sqlite3"
            )
            _fault("after-publication-exchange")
        elif not (candidate_at_canonical and source_at_private):
            raise RuntimeError("existing migration exchange position is ambiguous")
    os.fsync(database_fd)
    _fault("after-publication-database-fsync")
    os.fsync(migration_fd)
    _fault("after-publication-migration-fsync")
    if not _candidate_matches(authority, database_fd, "state.sqlite3", record):
        raise RuntimeError("promoted database does not match validated candidate")
    return _advance(record, "PROMOTED", promoted=True)


def _validate_promoted(
    authority: DataRootAuthority, record: dict[str, object]
) -> dict[str, object]:
    try:
        _fault("before-promoted-claim")
        if (
            authority._main_identity is None
            or not _same_identity(record["candidate_identity"], authority._main_identity)
        ):
            authority._promote_migration_candidate()
        _fault("after-promoted-claim")
    except BaseException as exc:
        raise _PromotedValidationError("CLAIM_REPLACEMENT") from exc
    if (
        authority._main_identity is None
        or not _same_identity(record["candidate_identity"], authority._main_identity)
    ):
        raise _PromotedValidationError("CLAIM_REPLACEMENT")
    try:
        vfs = authority._open_rooted_vfs()
    except BaseException as exc:
        raise _PromotedValidationError("NORMAL_VFS_OPEN") from exc
    try:
        _verify_database(vfs, _HEAD, phase6=True)
        _fault("after-promoted-normal-validation")
    except BaseException as exc:
        raise _PromotedValidationError("NORMAL_VFS_VALIDATION") from exc
    return _advance(
        record,
        "COMMITTED",
        normal_vfs_validated=True,
        canonical_identity=_identity_record(authority._main_identity),
    )


def _restore_existing(
    authority: DataRootAuthority, record: dict[str, object]
) -> dict[str, object]:
    database_fd = authority._database_dir_capability
    migration_fd = authority._directory_fd("database/migration")
    candidate = str(record["candidate_leaf"])
    candidate_at_canonical = _candidate_matches(
        authority, database_fd, "state.sqlite3", record
    )
    source_at_private = _source_matches(authority, migration_fd, candidate, record)
    source_at_canonical = _source_matches(
        authority, database_fd, "state.sqlite3", record
    )
    candidate_at_private = _candidate_matches(
        authority, migration_fd, candidate, record
    )
    if candidate_at_canonical:
        _require_no_sidecars(authority, database_fd, "state.sqlite3")
    if candidate_at_private or source_at_private:
        _require_no_sidecars(authority, migration_fd, candidate)
    if candidate_at_canonical and source_at_private:
        from bots5.infrastructure.attachments import _rename_exchange

        _fault("before-restore-exchange")
        _rename_exchange(migration_fd, candidate, database_fd, "state.sqlite3")
        _fault("after-restore-exchange")
    elif not (source_at_canonical and candidate_at_private):
        raise RuntimeError("migration restore exchange position is ambiguous")
    os.fsync(database_fd)
    _fault("after-restore-database-fsync")
    os.fsync(migration_fd)
    _fault("after-restore-migration-fsync")
    if not _source_matches(authority, database_fd, "state.sqlite3", record):
        raise RuntimeError("restored database does not match quiescent source")
    if not _candidate_matches(authority, migration_fd, candidate, record):
        raise RuntimeError("restored private candidate position is invalid")
    authority._replace_database_claim_from_canonical()
    _fault("after-restore-claim")
    if (
        authority._main_identity is None
        or not _same_identity(record["source_identity"], authority._main_identity)
    ):
        raise RuntimeError("restored source claim has the wrong identity")
    _verify_database(
        authority._open_rooted_vfs(), str(record["expected_start_revision"])
    )
    _fault("after-restore-validation")
    return _advance(
        record,
        "ROLLED_BACK",
        restored=True,
        normal_vfs_validated=True,
        canonical_identity=_identity_record(authority._main_identity),
    )


def _terminal_cleanup(
    authority: DataRootAuthority, record: dict[str, object]
) -> None:
    migration_fd = authority._directory_fd("database/migration")
    recovery_fd = authority._directory_fd("recovery")
    candidate = str(record["candidate_leaf"])
    if record["source_kind"] == "EXISTING":
        if _leaf_identity(authority, migration_fd, candidate + "-journal") is not None:
            raise RuntimeError("terminal migration retains a consequential sidecar")
        candidate_value = _leaf_identity(authority, migration_fd, candidate)
        if candidate_value is not None:
            expected = (
                _source_matches(authority, migration_fd, candidate, record)
                if record["phase"] == "COMMITTED"
                else _candidate_matches(authority, migration_fd, candidate, record)
            )
            if not expected:
                raise RuntimeError("terminal migration private database is not attributable")
            _remove_leaf(authority, migration_fd, candidate)
        backup_temp = str(record["backup_temp_leaf"])
        if _leaf_identity(authority, recovery_fd, backup_temp) is not None:
            _remove_leaf(authority, recovery_fd, backup_temp)
        backup = str(record["backup_leaf"])
        if _leaf_identity(authority, recovery_fd, backup) is not None:
            if not _recorded_file_matches(
                authority,
                recovery_fd,
                backup,
                identity=record["backup_identity"],
                size=record["backup_size"],
                sha256=record["backup_sha256"],
            ):
                raise RuntimeError("terminal migration backup is not attributable")
            _remove_leaf(authority, recovery_fd, backup)
    elif _leaf_identity(authority, migration_fd, candidate) is not None:
        raise RuntimeError("fresh migration candidate still exists after promotion")
    _clean_next_temp(authority, record)
    _remove_leaf(authority, migration_fd, _JOURNAL)


def _resume(authority: DataRootAuthority, record: dict[str, object]) -> None:
    _clean_next_temp(authority, record)
    _preflight_bundle(authority, record)
    phase = str(record["phase"])
    if record["source_kind"] == "EXISTING":
        authority._claim_database()
        if (
            not _same_identity(record["source_identity"], authority._main_identity)
            and phase not in {
                "SWITCH_INTENT", "PROMOTED", "COMMITTED", "RESTORE_INTENT",
                "ROLLED_BACK",
            }
        ):
            raise RuntimeError("migration source inode changed")
        if phase == "PREPARING":
            source_identity, source_hash = _quiesce_source(
                authority, str(record["expected_start_revision"])
            )
            record = _advance(
                record,
                "SOURCE_QUIESCED",
                recovered_revision=record["expected_start_revision"],
                source_size=source_identity.size,
                source_sha256=source_hash,
                source_quiesced=True,
            )
            _write_journal(authority, record)
            phase = str(record["phase"])
        if phase == "SOURCE_QUIESCED":
            record = _make_backup(authority, record)
            _write_journal(authority, record)
            phase = str(record["phase"])
    if phase in {"ABSENT", "BACKUP_VERIFIED", "MIGRATING_COPY"}:
        record = _build_candidate(authority, record)
        _write_journal(authority, record)
        phase = str(record["phase"])
    if phase in {"CANDIDATE_VALIDATED", "SWITCH_INTENT"}:
        record = _publish(authority, record)
        _write_journal(authority, record)
        phase = str(record["phase"])
    if phase == "PROMOTED":
        try:
            record = _validate_promoted(authority, record)
        except _PromotedValidationError as exc:
            if record["source_kind"] == "ABSENT":
                raise
            record = _advance(
                record,
                "RESTORE_INTENT",
                restore_primitive="RENAME_EXCHANGE",
                validation_failure_class=exc.failure_class,
            )
            _write_journal(authority, record)
            record = _restore_existing(authority, record)
            _write_journal(authority, record)
            _terminal_cleanup(authority, record)
            raise RuntimeError(
                "promoted migration candidate failed validation; prior source was restored"
            ) from exc
        else:
            _write_journal(authority, record)
            phase = str(record["phase"])
    if phase == "RESTORE_INTENT":
        record = _restore_existing(authority, record)
        _write_journal(authority, record)
        _terminal_cleanup(authority, record)
        raise RuntimeError(
            "migration recovery completed a prior-source restore; restart is required"
        )
    if phase == "ROLLED_BACK":
        if not _source_matches(
            authority, authority._database_dir_capability, "state.sqlite3", record
        ):
            raise RuntimeError("rolled-back migration source changed")
        if authority._main_identity is None or not _same_identity(
            record["canonical_identity"], authority._main_identity
        ):
            raise RuntimeError("rolled-back migration canonical identity changed")
        _verify_database(
            authority._open_rooted_vfs(), str(record["expected_start_revision"])
        )
        _terminal_cleanup(authority, record)
        raise RuntimeError(
            "migration recovery verified a prior-source rollback; restart is required"
        )
    if phase == "COMMITTED":
        if authority._main_claim is None:
            authority._claim_database()
        if (
            authority._main_identity is None
            or record.get("canonical_identity")
            != _identity_record(authority._main_identity)
        ):
            raise RuntimeError("committed migration canonical identity changed")
        if not _candidate_matches(
            authority, authority._database_dir_capability, "state.sqlite3", record
        ):
            raise RuntimeError("committed migration canonical content changed")
        _require_no_sidecars(
            authority, authority._database_dir_capability, "state.sqlite3"
        )
        _verify_database(authority._open_rooted_vfs(), _HEAD, phase6=True)
        _terminal_cleanup(authority, record)
        return
    raise RuntimeError(f"migration stopped in nonterminal phase: {phase}")


def upgrade_database(*, authority: DataRootAuthority) -> None:
    """Upgrade the authority's canonical database to the Phase 6 head."""
    if not isinstance(authority, DataRootAuthority):
        raise TypeError("upgrade_database requires DataRootAuthority")
    authority.assert_live()
    authority._begin_migration()
    success = False
    try:
        record = _read_journal(authority)
        if record is None:
            _clean_initial_temp(authority)
            database_fd = authority._database_dir_capability
            canonical = _leaf_identity(authority, database_fd, "state.sqlite3")
            transaction_id = str(uuid7())
            if canonical is None:
                base = _journal_base(authority, transaction_id, "ABSENT")
                record = _advance(base, "ABSENT")
                _write_journal(authority, record, initial=True)
            else:
                authority._claim_database()
                revision = _discover_existing(authority)
                if revision == _HEAD:
                    _quiesce_source(authority, _HEAD)
                    success = True
                    return
                base = _journal_base(authority, transaction_id, "EXISTING")
                base.update(
                    expected_start_revision=revision,
                    source_identity=_identity_record(authority._main_identity),
                    backup_leaf=f"migrate-{transaction_id}.backup.sqlite3",
                    backup_temp_leaf=f"migrate-{transaction_id}.backup.tmp",
                )
                record = _advance(base, "PREPARING")
                _write_journal(authority, record, initial=True)
        _resume(authority, record)
        success = True
    finally:
        authority._finish_migration(success=success)
