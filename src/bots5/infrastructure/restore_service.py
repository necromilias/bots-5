"""Slice D whole-installation restore: a journaled, authority-owned state machine.

This module implements the sealed Phase 9 Slice D design
(``RESTORE_STATE_MACHINE.md`` sections 1-5, ``DESTRUCTIVE_INVARIANTS.md``
I1-I18) as one linear-with-rollback journaled transaction executed under the
single ``DataRootAuthority`` (I3).  It owns: the durable journal, the
transaction lock, preservation of the pre-restore installation into the
``retained-installations`` fixed descendant (D-A=A.2, I2), private staging and
independent candidate validation before adoption (I1), the uncancellable
fsynced ``ADOPT_INTENT`` boundary, exactly one ``RENAME_EXCHANGE`` adoption
(I6), the rollback machine (D-E=E.1, I16), the restore receipt (section 8 of
the implementation spec), and restart reconciliation for the crash windows
(section 5 of the state machine).

Out of scope here: the startup coordinator wiring itself (which lives in
``bootstrap/desktop.py`` and delegates to the coordinator surface below).
Implemented here per the M5 milestone: the destructive-override *execution*
behind the M3 evaluation gate (D-D=D.2, I15), restored-GC tombstone
precedence driven through the existing Phase 6 GC recovery semantics
(D-F=F.2, I17), and the separate operator-directed removal of a preserved
installation (D-A=A.2) — the latter never invoked by the machine itself.

All filesystem access is descriptor-relative through ``DataRootAuthority``
capabilities.  Every private descriptor is entered in the authority claim
ledger.  Unknown, ambiguous or integrity-threatening outcomes halt and fail
closed (I5); nothing here ever guesses.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid as _uuid
import zipfile
from pathlib import Path
from typing import NoReturn
from uuid6 import uuid7

from bots5.core.errors import (
    BackupArchiveInvalid,
    BackupError,
    BackupUnclassifiedState,
    BackupUnsupported,
)
from bots5.domain.backup import canonical_backup_json
from bots5.infrastructure.attachments import _AttachmentFS, _rename_noreplace
from bots5.infrastructure.data_root_authority import (
    DataRootAuthority,
    DirectoryIdentityBaseline,
    FileIdentity,
    _check_directory,
    _check_regular,
    _identity,
)
from bots5.infrastructure.rooted_sqlite_vfs import (
    RootedSQLiteVfs,
    RootedVfsRegistrationCloseUnknown,
)


_RESTORE_JOURNAL_LEAF = ".bots5-restore-journal.json"
_RESTORE_RECEIPT_LEAF = ".bots5-restore-receipt.json"
_MAIN_LEAF = "state.sqlite3"
_JOURNAL_VERSION = 1
_JOURNAL_MAX_BYTES = 256 * 1024
_CHUNK = 1024 * 1024
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LOCK_LEAF = re.compile(rf"^\.bots5-restore-({_UUID})\.lock\Z")
_PAYLOAD = re.compile(r"payloads/sha256/([0-9a-f]{64})\Z")
_RECOVERY_ARTIFACT = re.compile(
    rf"recovery-artifacts/attachments/(staging|captures|gc)/({_UUID})\Z"
)
_SIDECARS = ("-journal", "-wal", "-shm")

#: The closed, strictly sequenced phase graph of the restore transaction
#: (``RESTORE_STATE_MACHINE.md`` section 1).  Sequence numbers are durable and
#: monotonic; the rollback and failed-closed phases always sequence above every
#: forward phase they can follow.
_PHASE_SEQUENCE = {
    "PRESERVING": 1,
    "PRESERVED": 2,
    "STAGING": 3,
    "STAGED_VALIDATED": 4,
    "ADOPT_INTENT": 5,
    "PROMOTED": 6,
    "POST_ADOPTION_VALIDATED": 7,
    "CLEANUP_PENDING": 8,
    "RESTORE_COMMITTED": 9,
    "RESTORE_ROLLBACK_INTENT": 10,
    "ROLLED_BACK": 11,
    "RESTORE_FAILED_CLOSED": 12,
}

_PHASE_PRIOR = {
    "PRESERVING": None,
    "PRESERVED": "PRESERVING",
    # STAGING is entered from PRESERVED by the normal preservation path and
    # directly from PRESERVING by the D-D=D.2 destructive-override path.  A
    # journal at STAGING (or beyond) with a null preservation_record is the
    # durable destructive marker: no rollback source exists for that
    # transaction and the rollback machine is never armed for it.
    "STAGING": {"PRESERVED", "PRESERVING"},
    "STAGED_VALIDATED": "STAGING",
    "ADOPT_INTENT": "STAGED_VALIDATED",
    "PROMOTED": "ADOPT_INTENT",
    "POST_ADOPTION_VALIDATED": "PROMOTED",
    "CLEANUP_PENDING": "POST_ADOPTION_VALIDATED",
    "RESTORE_COMMITTED": "CLEANUP_PENDING",
    "RESTORE_ROLLBACK_INTENT": {"PROMOTED", "POST_ADOPTION_VALIDATED"},
    "ROLLED_BACK": "RESTORE_ROLLBACK_INTENT",
    "RESTORE_FAILED_CLOSED": {
        "PRESERVING", "PRESERVED", "STAGING", "STAGED_VALIDATED",
        "ADOPT_INTENT", "PROMOTED", "POST_ADOPTION_VALIDATED",
        "CLEANUP_PENDING", "RESTORE_ROLLBACK_INTENT", "ROLLED_BACK",
    },
}


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("restore journal contains duplicate fields")
        result[key] = value
    return result

#: Pre-``ADOPT_INTENT`` phases are cancellable scratch: a crashed or abandoned
#: transaction here is discarded by reconciliation without live mutation.
_SCRATCH_PHASES = frozenset({"PRESERVING", "PRESERVED", "STAGING", "STAGED_VALIDATED"})

#: Phases a destructive (D-D=D.2) transaction — a journal whose
#: ``preservation_record`` is null — can never legitimately reach: there is no
#: preservation to record and the rollback machine is never armed for it.  A
#: journal observed in one of these states without a preservation record is
#: unattributable and fails closed.
_DESTRUCTIVE_FORBIDDEN_PHASES = frozenset(
    {"PRESERVED", "RESTORE_ROLLBACK_INTENT", "ROLLED_BACK"}
)

_JOURNAL_FIELDS = frozenset(
    {
        "journal_version",
        "restore_transaction_id",
        "backup_id",
        "phase",
        "sequence",
        "package_path",
        "package_sha256",
        "target_db_revision",
        "candidate_leaf",
        "displaced_leaf",
        "preservation_record",
        # The durable publication plan (B2/B3 repair): the exact CAS payload
        # digests and lifecycle artefacts this transaction is authorized to
        # publish into the live attachment namespaces, journaled at STAGING
        # BEFORE any live namespace mutation.  ``None`` means publication was
        # never authorized, so nothing needs withdrawing on abort.
        "staged_attachment_state",
        "directory_identities",
    }
)

#: The closed field set of a journaled staged-attachment publication plan.
_STAGED_ATTACHMENT_KEYS = frozenset({"objects", "gc", "staging", "captures"})

_RECEIPT_FIELDS = (
    "backup_id",
    "package_sha256",
    "source_revision",
    "target_revision_at_commit",
    "post_adoption_migration",
    "target_revision_after_migration",
    "migration_recovery_reference",
    "transaction_id",
    "outcome",
)

#: The closed post-adoption migration state enum (spec section 8).  Only the
#: ``required`` state is finalised, to ``completed`` or ``failed_rolled_back``
#: (RESTORE_STATE_MACHINE.md section 3.1).
_RECEIPT_MIGRATION_STATES = frozenset(
    {"not_required", "required", "completed", "failed_rolled_back"}
)

_IDENTITY_KEYS = frozenset(
    {
        "device_major",
        "device_minor",
        "inode",
        "mount_id",
        "type",
        "uid",
        "mode",
        "nlink",
    }
)

_PRESERVATION_KEYS = frozenset({"mode", "path", "statx_identity", "sha256", "bytes"})

_TEST_FAULT_HOOK = None


def _fault(point: str) -> None:
    hook = _TEST_FAULT_HOOK
    if hook is not None:
        hook(point)


def _identity_record(value: FileIdentity) -> dict[str, object]:
    return {
        "device_major": value.device_major,
        "device_minor": value.device_minor,
        "mount_id": value.mount_id,
        "inode": value.inode,
        "type": "directory" if stat.S_ISDIR(value.mode) else "regular",
        "uid": value.uid,
        "mode": stat.S_IMODE(value.mode),
        "nlink": value.nlink,
    }


def _same_identity(record: object, value: FileIdentity) -> bool:
    return record == _identity_record(value)


def _uuid7_text(value: object) -> bool:
    if type(value) is not str or re.fullmatch(_UUID, value) is None:
        return False
    try:
        parsed = _uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 7 and str(parsed) == value


def _sha256_text(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _journal_size_ceiling(record: dict[str, object]) -> int:
    """Largest canonical journal size any later state of this transaction
    can reach.

    After the STAGING publication-plan authorization the journal record's
    content changes only by phase and sequence until the journal is
    unlinked (the plan and every other field are fixed), so the widest
    phase name and the longest sequence encoding bound every remaining
    journal write of the transaction — including the rollback and
    failed-closed phases (+17 bytes over the STAGING authorization, reached
    by ``RESTORE_ROLLBACK_INTENT``).  Authorizing against this ceiling
    instead of the authorized size itself is what makes the write-time
    size cap unobservable for a legitimate transaction: no later write of
    an authorized transaction can be refused mid-flight, after the
    adoption boundary.
    """
    widest = dict(record)
    widest["phase"] = max(_PHASE_SEQUENCE, key=len)
    widest["sequence"] = max(_PHASE_SEQUENCE.values())
    return len(canonical_backup_json(widest))


class RestoreService:
    """Whole-installation restore transaction for one authoritative data root."""

    def __init__(self, authority: DataRootAuthority) -> None:
        self._authority = authority

    # ------------------------------------------------------------------
    # Descriptor ownership helpers (I3: every private fd is ledgered).
    # ------------------------------------------------------------------

    def _own_fd(self, label: str, fd: int):
        return self._authority._claim_scoped_fd(f"restore:{label}", fd)

    def _release_fd(self, claim) -> None:
        self._authority._release_scoped_fd(claim)

    def _database_fd(self) -> int:
        return self._authority._database_dir_capability

    def _open_regular(
        self, directory_fd: int, leaf: str, *, flags: int = os.O_RDONLY
    ) -> tuple[object, int, FileIdentity]:
        fd = os.open(
            leaf,
            flags | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        claim = self._own_fd(f"regular:{leaf}", fd)
        try:
            value = _check_regular(fd, mount_id=self._authority._root_identity.mount_id)
            return claim, fd, value
        except BaseException:
            self._release_fd(claim)
            raise

    def _hash_fd(self, fd: int) -> tuple[FileIdentity, str, int]:
        before = _identity(fd)
        digest = hashlib.sha256()
        offset = 0
        while offset < before.size:
            chunk = os.pread(fd, min(_CHUNK, before.size - offset), offset)
            if not chunk:
                raise BackupUnclassifiedState(
                    "restore artefact became truncated while being hashed"
                )
            digest.update(chunk)
            offset += len(chunk)
        after = _identity(fd)
        if before != after:
            raise BackupUnclassifiedState(
                "restore artefact changed while being hashed"
            )
        return after, digest.hexdigest(), int(after.size)

    def _write_all_fd(self, fd: int, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise BackupError("restore staging write was incomplete")
            offset += written

    def _hash_leaf(
        self, directory_fd: int, leaf: str
    ) -> tuple[FileIdentity, str, int]:
        claim, fd, _ = self._open_regular(directory_fd, leaf)
        try:
            return self._hash_fd(fd)
        finally:
            self._release_fd(claim)

    # ------------------------------------------------------------------
    # Atomic private-file publication (canonical JSON, spec section 1).
    # ------------------------------------------------------------------

    def _atomic_write_private(
        self, directory_fd: int, temp_leaf: str, final_leaf: str, payload: bytes
    ) -> None:
        # A crash between the O_EXCL temp creation and the rename leaves the
        # deterministic temp leaf behind; every later re-attempt of this same
        # publication would otherwise fail on it forever (I4).  The leaf is
        # namespaced by this transaction (temp_leaf embeds the txid), holds no
        # content the machine ever reads back, and can only be debris of an
        # interrupted publication of this same leaf, so it is removed — and the
        # directory fsynced — before the rewrite.  The published file is then
        # provably the intended content: freshly created O_EXCL, fully written
        # from the current payload, re-stat-proven, fsynced, and renamed.
        try:
            os.unlink(temp_leaf, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        else:
            os.fsync(directory_fd)
        fd = os.open(
            temp_leaf,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        claim = self._own_fd(f"private-write:{temp_leaf}", fd)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    raise BackupError("restore private write was incomplete")
                offset += written
            os.fsync(fd)
            value = os.fstat(fd)
            expected = _identity(fd)
            if (
                value.st_size != len(payload)
                or not stat.S_ISREG(expected.mode)
                or expected.uid != os.geteuid()
                or stat.S_IMODE(expected.mode) != 0o600
                or expected.nlink != 1
            ):
                raise BackupError("restore private write failed its re-stat proof")
        finally:
            self._release_fd(claim)
        os.rename(temp_leaf, final_leaf, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)

    # ------------------------------------------------------------------
    # Durable journal (RESTORE_STATE_MACHINE.md section 2).
    # ------------------------------------------------------------------

    def _base_record(
        self,
        *,
        txid: str,
        backup_id: str,
        package_path: str,
        package_sha256: str,
        target_db_revision: str,
    ) -> dict[str, object]:
        candidate_leaf = f".bots5-restore-{txid}.candidate.sqlite3"
        return {
            "journal_version": _JOURNAL_VERSION,
            "restore_transaction_id": txid,
            "backup_id": backup_id,
            "phase": "PRESERVING",
            "sequence": _PHASE_SEQUENCE["PRESERVING"],
            "package_path": package_path,
            "package_sha256": package_sha256,
            "target_db_revision": target_db_revision,
            "candidate_leaf": candidate_leaf,
            "displaced_leaf": candidate_leaf,
            "preservation_record": None,
            "staged_attachment_state": None,
            "directory_identities": self._directory_identity_records(),
        }

    def _directory_identity_records(self) -> dict[str, dict[str, object]]:
        authority = self._authority
        return {
            "root": _identity_record(authority._root_identity),
            "database": _identity_record(_identity(authority._database_dir_capability)),
            "attachments": _identity_record(
                _identity(authority._attachments_dir_capability)
            ),
            "recovery": _identity_record(
                _identity(authority._directory_fd("recovery"))
            ),
        }

    def _advance(
        self, record: dict[str, object], phase: str, **updates: object
    ) -> dict[str, object]:
        prior = record["phase"]
        expected = _PHASE_PRIOR[phase]
        if isinstance(expected, set):
            if prior not in expected:
                raise BackupError(
                    f"restore phase transition is invalid: {prior} -> {phase}"
                )
        elif prior != expected:
            raise BackupError(
                f"restore phase transition is invalid: {prior} -> {phase}"
            )
        next_record = dict(record)
        next_record.update(updates)
        next_record["phase"] = phase
        next_record["sequence"] = _PHASE_SEQUENCE[phase]
        return next_record

    def _write_journal(self, record: dict[str, object]) -> None:
        payload = canonical_backup_json(record)
        # Refuse to durably publish anything the reader would reject.  The
        # size cap is enforced HERE, at every write, before any byte reaches
        # the filesystem (I4): an oversized journal would be refused by
        # ``_read_journal`` on every later startup and permanently wedge the
        # data root.  The refusal leaves the durable journal at its previous
        # reconcilable phase (or absent), so the refusal alone can never
        # create an unrecoverable state; the STAGING authorization below
        # budgets the whole remaining transaction (``_journal_size_ceiling``),
        # so a legitimate restore is never refused mid-flight.
        if len(payload) > _JOURNAL_MAX_BYTES:
            raise BackupError(
                "restore journal exceeds its size limit; the write is "
                "refused before publication"
            )
        self._validate_journal_object(record, payload)
        database_fd = self._database_fd()
        temp_leaf = (
            f".bots5-restore-journal-{record['restore_transaction_id']}"
            f"-{record['sequence']}.tmp"
        )
        _fault(f"before-journal-write-{record['phase']}")
        self._atomic_write_private(database_fd, temp_leaf, _RESTORE_JOURNAL_LEAF, payload)
        _fault(f"after-journal-write-{record['phase']}")

    def _read_journal(self) -> tuple[dict[str, object], bytes] | None:
        database_fd = self._database_fd()
        try:
            claim, fd, value = self._open_regular(database_fd, _RESTORE_JOURNAL_LEAF)
        except FileNotFoundError:
            return None
        try:
            if value.size > _JOURNAL_MAX_BYTES:
                raise BackupUnclassifiedState("restore journal exceeds its size limit")
            raw = bytearray()
            total = 0
            while True:
                chunk = os.read(fd, _CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > _JOURNAL_MAX_BYTES:
                    raise BackupUnclassifiedState("restore journal exceeds its size limit")
                raw.extend(chunk)
        finally:
            self._release_fd(claim)
        try:
            record = json.loads(bytes(raw), object_pairs_hook=_reject_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise BackupUnclassifiedState(
                "restore journal is torn or non-canonical and is not trusted"
            ) from exc
        # A torn or non-canonical journal is never partially trusted (W10):
        # canonical bytes must reproduce the observed bytes exactly.
        payload = canonical_backup_json(record)
        if payload != bytes(raw):
            raise BackupUnclassifiedState(
                "restore journal is torn or non-canonical and is not trusted"
            )
        self._validate_journal_object(record, payload)
        return record, bytes(raw)

    def _unlink_journal(self) -> None:
        database_fd = self._database_fd()
        try:
            os.unlink(_RESTORE_JOURNAL_LEAF, dir_fd=database_fd)
        except FileNotFoundError:
            pass
        os.fsync(database_fd)

    def _validate_journal_object(
        self, record: object, payload: bytes | None = None
    ) -> None:
        if not isinstance(record, dict) or set(record) != _JOURNAL_FIELDS:
            raise BackupUnclassifiedState(
                "restore journal is not the closed journal schema"
            )
        if record["journal_version"] != _JOURNAL_VERSION:
            raise BackupUnclassifiedState("restore journal version is unsupported")
        phase = record["phase"]
        if phase not in _PHASE_SEQUENCE:
            raise BackupUnclassifiedState("restore journal phase is invalid")
        if record["sequence"] != _PHASE_SEQUENCE[phase]:
            raise BackupUnclassifiedState("restore journal sequence is invalid")
        txid = record["restore_transaction_id"]
        if not _uuid7_text(txid):
            raise BackupUnclassifiedState("restore journal transaction id is invalid")
        if not _uuid7_text(record["backup_id"]):
            raise BackupUnclassifiedState("restore journal backup id is invalid")
        candidate_leaf = f".bots5-restore-{txid}.candidate.sqlite3"
        if record["candidate_leaf"] != candidate_leaf or record["displaced_leaf"] != candidate_leaf:
            raise BackupUnclassifiedState("restore journal database leaves are invalid")
        package_path = record["package_path"]
        if (
            type(package_path) is not str
            or not os.path.isabs(package_path)
            or "\x00" in package_path
            or ".." in Path(package_path).parts
        ):
            raise BackupUnclassifiedState("restore journal package path is invalid")
        if not _sha256_text(record["package_sha256"]):
            raise BackupUnclassifiedState("restore journal package digest is invalid")
        from bots5.infrastructure.persistence.migration_runner import _MIGRATION_CHAIN

        if record["target_db_revision"] not in _MIGRATION_CHAIN:
            raise BackupUnclassifiedState("restore journal target revision is unsupported")
        preservation = record["preservation_record"]
        if phase == "PRESERVING":
            if preservation is not None:
                raise BackupUnclassifiedState(
                    "restore journal preservation record is premature"
                )
        elif preservation is None:
            # Destructive (D-D=D.2) transaction: the null preservation record
            # is its durable marker.  It can never reach a phase that exists
            # only on the preservation/rollback path.
            if phase in _DESTRUCTIVE_FORBIDDEN_PHASES:
                raise BackupUnclassifiedState(
                    "restore journal preservation record is required for this phase"
                )
        else:
            self._validate_preservation_record(preservation, record)
        self._validate_staged_attachment_state(record)
        identities = record["directory_identities"]
        if not isinstance(identities, dict) or set(identities) != {
            "root", "database", "attachments", "recovery"
        }:
            raise BackupUnclassifiedState("restore journal directory identities are invalid")
        observed = self._directory_identity_records()
        for name, value in identities.items():
            self._validate_identity_record(value, expected_type="directory")
            if value != observed[name]:
                raise BackupUnclassifiedState(
                    f"restore journal {name} identity no longer matches the authority"
                )
        if payload is not None and canonical_backup_json(record) != payload:
            raise BackupUnclassifiedState("restore journal is not canonical JSON")

    def _validate_preservation_record(
        self, value: object, record: dict[str, object]
    ) -> None:
        if not isinstance(value, dict) or set(value) != _PRESERVATION_KEYS:
            raise BackupUnclassifiedState(
                "restore journal preservation record is malformed"
            )
        txid = record["restore_transaction_id"]
        expected_path = (
            f"retained-installations/{txid}-{record['backup_id']}.sqlite3"
        )
        if value["mode"] != 0o600 or value["path"] != expected_path:
            raise BackupUnclassifiedState(
                "restore journal preservation record location is invalid"
            )
        if not _sha256_text(value["sha256"]):
            raise BackupUnclassifiedState(
                "restore journal preservation digest is invalid"
            )
        if type(value["bytes"]) is not int or value["bytes"] < 1:
            raise BackupUnclassifiedState(
                "restore journal preservation size is invalid"
            )
        self._validate_identity_record(value["statx_identity"], expected_type="regular")

    def _validate_staged_attachment_state(self, record: dict[str, object]) -> None:
        """Validate the journaled publication plan (closed schema, sorted)."""
        plan = record["staged_attachment_state"]
        if plan is None:
            # Publication was never authorized (pre-STAGING phase), or the
            # transaction crashed between the STAGING journal write and the
            # authorization write — in both states nothing has been
            # published into the live namespaces.
            return
        if not isinstance(plan, dict) or set(plan) != _STAGED_ATTACHMENT_KEYS:
            raise BackupUnclassifiedState(
                "restore journal staged attachment state is malformed"
            )
        objects = plan["objects"]
        if (
            not isinstance(objects, list)
            or any(not _sha256_text(digest) for digest in objects)
            or sorted(objects) != objects
            or len(set(objects)) != len(objects)
        ):
            raise BackupUnclassifiedState(
                "restore journal staged payload plan is malformed"
            )
        for namespace in ("gc", "staging", "captures"):
            entries = plan[namespace]
            if not isinstance(entries, list):
                raise BackupUnclassifiedState(
                    "restore journal staged lifecycle plan is malformed"
                )
            leaves: list[str] = []
            for entry in entries:
                if not isinstance(entry, list) or len(entry) != 3:
                    raise BackupUnclassifiedState(
                        "restore journal staged lifecycle plan is malformed"
                    )
                leaf, sha, size = entry
                if (
                    not _uuid7_text(leaf)
                    or not _sha256_text(sha)
                    or type(size) is not int
                    or size < 0
                ):
                    raise BackupUnclassifiedState(
                        "restore journal staged lifecycle plan is malformed"
                    )
                leaves.append(leaf)
            if sorted(leaves) != leaves or len(set(leaves)) != len(leaves):
                raise BackupUnclassifiedState(
                    "restore journal staged lifecycle plan is malformed"
                )

    def _validate_identity_record(self, value: object, *, expected_type: str) -> None:
        if not isinstance(value, dict) or set(value) != _IDENTITY_KEYS:
            raise BackupUnclassifiedState("restore journal identity record is malformed")
        if value["type"] != expected_type:
            raise BackupUnclassifiedState("restore journal identity type is malformed")
        for key in _IDENTITY_KEYS - {"type"}:
            item = value[key]
            if type(item) is not int or item < 0:
                raise BackupUnclassifiedState("restore journal identity value is malformed")
        if (
            value["uid"] != os.geteuid()
            or value["mode"] != (0o700 if expected_type == "directory" else 0o600)
            or (expected_type == "regular" and value["nlink"] != 1)
        ):
            raise BackupUnclassifiedState("restore journal identity policy is malformed")

    # ------------------------------------------------------------------
    # Transaction lock (I11).
    # ------------------------------------------------------------------

    def _lock_leaf(self, txid: str) -> str:
        if not _uuid7_text(txid):
            raise BackupUnclassifiedState("restore transaction id is invalid")
        return f".bots5-restore-{txid}.lock"

    def _open_transaction_lock(self, txid: str):
        leaf = self._lock_leaf(txid)
        database_fd = self._database_fd()
        fd = os.open(
            leaf,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=database_fd,
        )
        claim = self._own_fd(f"transaction-lock:{txid}", fd)
        try:
            _check_regular(fd, mount_id=self._authority._root_identity.mount_id)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._release_fd(claim)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise BackupError(
                    "restore transaction lock is held; a restore transaction may be live"
                ) from exc
            raise BackupError("restore transaction lock cannot be acquired") from exc
        except BaseException:
            self._release_fd(claim)
            raise
        return claim

    def _release_transaction_lock(self, claim, leaf: str, *, unlink: bool) -> None:
        if unlink:
            database_fd = self._database_fd()
            try:
                os.unlink(leaf, dir_fd=database_fd)
            except FileNotFoundError:
                pass
            os.fsync(database_fd)
        self._release_fd(claim)

    def _sweep_orphan_locks(self) -> None:
        """Attribute lock files without a journal (W1 scratch, I11)."""
        database_fd = self._database_fd()
        try:
            names = self._authority.fresh_directory_inventory("database")
        except BaseException as exc:
            raise BackupUnclassifiedState(
                "restore orphan-lock sweep cannot observe the database directory"
            ) from exc
        for name in sorted(names):
            match = _LOCK_LEAF.fullmatch(name)
            if match is None:
                continue
            txid = match.group(1)
            fd = os.open(
                name,
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=database_fd,
            )
            claim = self._own_fd(f"orphan-lock:{txid}", fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                self._release_fd(claim)
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise BackupError(
                        "restore transaction lock is held; a restore transaction may be live"
                    ) from exc
                raise BackupError(
                    "restore orphan transaction lock cannot be attributed"
                ) from exc
            self._release_fd(claim)
            os.unlink(name, dir_fd=database_fd)
            os.fsync(database_fd)

    # ------------------------------------------------------------------
    # Quiescence and preservation (machine section 3 step 2, D-A=A.2, I2).
    # ------------------------------------------------------------------

    def _quiesce(self) -> None:
        authority = self._authority
        authority._claim_database()
        database_fd = self._database_fd()
        names = set(authority.fresh_directory_inventory("database"))
        residue = sorted(
            name
            for name in names
            if any(name == f"{_MAIN_LEAF}{suffix}" for suffix in _SIDECARS)
        )
        if residue:
            raise BackupUnclassifiedState(
                "live database carries SQLite sidecar residue: " + ", ".join(residue)
            )
        with authority._condition:
            checked_out = authority._checked_out_connections
        if checked_out:
            raise BackupError(
                "restore requires a quiesced installation; database connections remain checked out"
            )
        authority.database_durability_fence()

    def _preservation_leaf(self, record: dict[str, object]) -> str:
        return f"{record['restore_transaction_id']}-{record['backup_id']}.sqlite3"

    def _preserve(self, record: dict[str, object]) -> dict[str, object]:
        authority = self._authority
        retained_fd = authority._retained_installations_dir_capability
        leaf = self._preservation_leaf(record)
        main_fd = authority._claim_database()
        before = _identity(main_fd)
        if before.size < 1:
            raise BackupError("live database cannot be preserved: it is empty")
        digest = hashlib.sha256()
        copied = 0
        artefact_fd = os.open(
            leaf,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
            dir_fd=retained_fd,
        )
        claim = self._own_fd(f"preservation:{leaf}", artefact_fd)
        try:
            offset = 0
            while offset < before.size:
                block = os.pread(main_fd, min(_CHUNK, before.size - offset), offset)
                if not block:
                    raise BackupError("live database changed while being preserved")
                digest.update(block)
                copied += len(block)
                written = os.write(artefact_fd, block)
                if written != len(block):
                    raise BackupError("preservation write was incomplete")
                offset += len(block)
            os.fsync(artefact_fd)
            after = _identity(main_fd)
            if after != before or copied != before.size:
                raise BackupError(
                    "live database changed while being preserved; restoration halted"
                )
            artefact_identity, artefact_sha, artefact_size = self._hash_fd(artefact_fd)
            if (
                artefact_sha != digest.hexdigest()
                or artefact_size != before.size
                or artefact_identity.size != before.size
            ):
                raise BackupError(
                    "preservation artefact does not match the live database bytes"
                )
        except BaseException:
            try:
                os.unlink(leaf, dir_fd=retained_fd)
                os.fsync(retained_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            self._release_fd(claim)
        os.fsync(retained_fd)
        return {
            "mode": 0o600,
            "path": f"retained-installations/{leaf}",
            "statx_identity": _identity_record(artefact_identity),
            "sha256": artefact_sha,
            "bytes": artefact_size,
        }

    def _prove_rollback_source(self, record: dict[str, object]) -> None:
        """DEFECT-9: the rollback source must be present and identity-recorded."""
        preservation = record["preservation_record"]
        assert preservation is not None
        retained_fd = self._authority._retained_installations_dir_capability
        leaf = self._preservation_leaf(record)
        try:
            artefact_identity, artefact_sha, artefact_size = self._hash_leaf(
                retained_fd, leaf
            )
        except FileNotFoundError as exc:
            raise BackupError(
                "rollback source is missing before the adoption boundary"
            ) from exc
        if (
            not _same_identity(preservation["statx_identity"], artefact_identity)
            or preservation["sha256"] != artefact_sha
            or preservation["bytes"] != artefact_size
        ):
            raise BackupError(
                "rollback source does not match its recorded preservation identity"
            )
        main_fd = self._authority._claim_database()
        _, live_sha, live_size = self._hash_fd(main_fd)
        if live_sha != preservation["sha256"] or live_size != preservation["bytes"]:
            raise BackupError(
                "live database no longer matches the preserved rollback source"
            )

    # ------------------------------------------------------------------
    # Staging and candidate validation (machine section 3 steps 3-4, I1).
    # ------------------------------------------------------------------

    def _hash_package_file(self, package_path: Path) -> str:
        fd = os.open(package_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode):
                raise BackupArchiveInvalid("restore package is not a regular file")
            digest = hashlib.sha256()
            offset = 0
            while offset < value.st_size:
                block = os.pread(fd, _CHUNK, offset)
                if not block:
                    raise BackupArchiveInvalid("restore package is truncated")
                digest.update(block)
                offset += len(block)
        finally:
            os.close(fd)
        return digest.hexdigest()

    def _package_manifest(self, package_path: Path) -> dict[str, object]:
        try:
            with zipfile.ZipFile(package_path) as package:
                manifest = json.loads(package.read("manifest.json"))
        except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
            raise BackupArchiveInvalid(
                "restore package manifest cannot be read"
            ) from exc
        if not isinstance(manifest, dict):
            raise BackupArchiveInvalid("restore package manifest is malformed")
        return manifest

    def _manifest_payloads(
        self, manifest: dict[str, object]
    ) -> dict[str, int]:
        payloads: dict[str, int] = {}
        for row in manifest["entry_inventory"]:
            path = str(row["path"])
            match = _PAYLOAD.fullmatch(path)
            if match is None:
                continue
            digest = match.group(1)
            size = int(row["uncompressed_size"])
            if digest in payloads:
                raise BackupArchiveInvalid("restore package repeats a payload digest")
            payloads[digest] = size
        return payloads

    def _manifest_recovery_artifacts(
        self, manifest: dict[str, object]
    ) -> dict[tuple[str, str], tuple[int, str]]:
        """Closed parsing of the package's attachment recovery artefacts.

        Backup v1 packages may carry ``recovery-artifacts/attachments/
        {staging,captures,gc}/<uuid7>`` entries captured from a source whose
        attachment lifecycle was mid-recovery (D-F=F.2).  Each entry must
        carry a manifest sha256 and size and must not repeat a leaf within
        its namespace.
        """
        artifacts: dict[tuple[str, str], tuple[int, str]] = {}
        for row in manifest["entry_inventory"]:
            path = str(row["path"])
            match = _RECOVERY_ARTIFACT.fullmatch(path)
            if match is None:
                continue
            namespace, leaf = match.group(1), match.group(2)
            size = int(row["uncompressed_size"])
            sha = str(row["sha256"])
            if not _sha256_text(sha):
                raise BackupArchiveInvalid(
                    "restore package recovery artefact digest is malformed"
                )
            if (namespace, leaf) in artifacts:
                raise BackupArchiveInvalid(
                    "restore package repeats a recovery artefact leaf"
                )
            artifacts[(namespace, leaf)] = (size, sha)
        return artifacts

    def _stage_package(
        self,
        record: dict[str, object],
        package_path: Path,
        package_sha256: str,
    ) -> tuple[FileIdentity, dict[str, int]]:
        observed = self._hash_package_file(package_path)
        if observed != package_sha256:
            raise BackupArchiveInvalid(
                "restore package changed after independent verification"
            )
        manifest = self._package_manifest(package_path)
        capture = manifest["capture"]
        payloads = self._manifest_payloads(manifest)
        artifacts = self._manifest_recovery_artifacts(manifest)
        candidate_identity = self._extract_candidate(
            record, package_path, str(capture["source_database_size"]),
            str(capture["source_database_sha256"]),
        )
        # D-F=F.2: prove the restored authoritative database, the target
        # attachment namespaces and every package recovery artefact form one
        # mutually attributable recovery state before adoption.  The target
        # observation is taken before any payload staging so it proves the
        # pre-existing state; ambiguous evidence fails closed before any
        # live mutation.
        state = self._candidate_attachment_state(record)
        observed = self._observe_target_attachment_namespaces()
        # Authorize publication durably BEFORE any live namespace mutation:
        # the plan records exactly the payloads and lifecycle artefacts this
        # transaction may publish, so an abort can withdraw precisely those
        # and nothing else (B2/B3).  A crash before this write leaves the
        # plan ``None`` in the journal — and nothing has been published.
        plan = self._plan_staged_attachment_state(payloads, artifacts, observed)
        self._authorize_staged_attachment_state(record, plan)
        self._stage_payloads(
            record, package_path, payloads, present=observed["objects"]
        )
        self._attribute_and_stage_recovery_state(
            record, state, observed, package_path, payloads, artifacts
        )
        return candidate_identity, payloads

    def _extract_candidate(
        self,
        record: dict[str, object],
        package_path: Path,
        expected_size_text: str,
        expected_sha_text: str,
    ) -> FileIdentity:
        leaf = str(record["candidate_leaf"])
        database_fd = self._database_fd()
        expected_size = int(expected_size_text)
        expected_sha = expected_sha_text
        if not _sha256_text(expected_sha):
            raise BackupArchiveInvalid("restore package database digest is malformed")
        with zipfile.ZipFile(package_path) as package:
            info = package.getinfo("database/state.sqlite3")
            fd = os.open(
                leaf,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | os.O_NOFOLLOW,
                0o600,
                dir_fd=database_fd,
            )
            claim = self._own_fd(f"candidate:{leaf}", fd)
            digest = hashlib.sha256()
            size = 0
            try:
                with package.open(info, "r") as source:
                    while block := source.read(_CHUNK):
                        digest.update(block)
                        size += len(block)
                        self._write_all_fd(fd, block)
                os.fsync(fd)
                identity = _identity(fd)
            finally:
                self._release_fd(claim)
        os.fsync(database_fd)
        if size != expected_size or digest.hexdigest() != expected_sha:
            raise BackupArchiveInvalid(
                "staged candidate database does not match the package manifest"
            )
        if (
            not stat.S_ISREG(identity.mode)
            or identity.uid != os.geteuid()
            or stat.S_IMODE(identity.mode) != 0o600
            or identity.nlink != 1
        ):
            raise BackupUnclassifiedState("staged candidate has an unsafe identity")
        return identity

    def _stage_payloads(
        self,
        record: dict[str, object],
        package_path: Path,
        payloads: dict[str, int],
        *,
        present: frozenset[str],
    ) -> None:
        """Publish the package payloads into the live CAS.

        ``present`` is the pre-staging observation of ``attachments/objects``
        shared with the journaled publication plan: a payload already present
        is verified against the package bytes and never re-published, so the
        plan (manifest minus this set) is exactly what this transaction can
        add to the live CAS.
        """
        authority = self._authority
        staging_fd = authority._directory_fd("attachments/staging")
        objects_fd = authority._directory_fd("attachments/objects")
        staging_leaf = f"restore-{record['restore_transaction_id']}"
        try:
            os.mkdir(staging_leaf, 0o700, dir_fd=staging_fd)
        except FileExistsError as exc:
            raise BackupUnclassifiedState(
                "restore payload staging leaf already exists"
            ) from exc
        dir_fd = os.open(
            staging_leaf,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=staging_fd,
        )
        dir_claim = self._own_fd(f"payload-staging:{staging_leaf}", dir_fd)
        mutable_present = set(present)
        try:
            with zipfile.ZipFile(package_path) as package:
                for digest, size in sorted(payloads.items()):
                    info = package.getinfo(f"payloads/sha256/{digest}")
                    fd = os.open(
                        digest,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=dir_fd,
                    )
                    claim = self._own_fd(f"staged-payload:{digest}", fd)
                    actual = hashlib.sha256()
                    copied = 0
                    try:
                        with package.open(info, "r") as source:
                            while block := source.read(_CHUNK):
                                actual.update(block)
                                copied += len(block)
                                self._write_all_fd(fd, block)
                        os.fsync(fd)
                    finally:
                        self._release_fd(claim)
                    if copied != size or actual.hexdigest() != digest:
                        raise BackupArchiveInvalid(
                            "staged payload does not match the package manifest"
                        )
                    os.fsync(dir_fd)
                    if digest in mutable_present:
                        self._verify_object_leaf(objects_fd, digest, size)
                        os.unlink(digest, dir_fd=dir_fd)
                        os.fsync(dir_fd)
                    else:
                        _rename_noreplace(dir_fd, digest, objects_fd, digest)
                        mutable_present.add(digest)
        finally:
            self._release_fd(dir_claim)
        os.fsync(objects_fd)
        os.fsync(staging_fd)

    def _verify_object_leaf(self, objects_fd: int, digest: str, size: int) -> None:
        """HS-1/I5: an existing canonical object must match the package bytes."""
        claim, fd, _ = self._open_regular(objects_fd, digest)
        try:
            identity, sha, byte_size = self._hash_fd(fd)
        finally:
            self._release_fd(claim)
        if sha != digest or byte_size != size or identity.size != size:
            self._authority.poison(
                "restore staging found a content mismatch for an existing canonical object"
            )
            raise BackupUnclassifiedState(
                "existing canonical object does not match the restore package"
            )

    # ------------------------------------------------------------------
    # Staged-attachment publication plan and withdrawal (B2/B3 repair).
    # Payloads and package recovery artefacts are published into the LIVE
    # attachment namespaces before adoption, so an aborted or crashed
    # transaction must withdraw exactly what it published — by recorded
    # identity/digest alone — to return the pre-restore installation to a
    # bootable state.  The plan is journaled at STAGING before any live
    # namespace mutation and is propagated into the record every later
    # journal write publishes, so it stays durable from STAGING through
    # every abort/rollback phase; withdrawal never touches a leaf outside
    # the plan (pre-existing objects and lifecycle artefacts are excluded
    # at planning time) and never deletes content it cannot prove.
    # ------------------------------------------------------------------

    def _plan_staged_attachment_state(
        self,
        payloads: dict[str, int],
        artifacts: dict[tuple[str, str], tuple[int, str]],
        observed: dict[str, frozenset[str]],
    ) -> dict[str, object]:
        """The exact live-namespace publications this transaction may make.

        A package payload or recovery artefact already present in the target
        namespace is verified in place (never re-published), so the plan is
        the manifest minus the pre-staging observation — i.e. exactly the
        leaves this transaction can add.  Every entry is identity-checkable
        without the package: CAS digests are content-addressed, and every
        lifecycle entry records the published bytes' sha256 and size.
        """
        return {
            "objects": sorted(
                digest for digest in payloads if digest not in observed["objects"]
            ),
            "gc": sorted(
                [leaf, sha, size]
                for (namespace, leaf), (size, sha) in artifacts.items()
                if namespace == "gc" and leaf not in observed["gc"]
            ),
            "staging": sorted(
                [leaf, sha, size]
                for (namespace, leaf), (size, sha) in artifacts.items()
                if namespace == "staging" and leaf not in observed["staging"]
            ),
            "captures": sorted(
                [leaf, sha, size]
                for (namespace, leaf), (size, sha) in artifacts.items()
                if namespace == "captures" and leaf not in observed["captures"]
            ),
        }

    def _authorize_staged_attachment_state(
        self, record: dict[str, object], plan: dict[str, object]
    ) -> None:
        """Journal the publication plan; refuse before any live mutation if
        the transaction cannot fit the journal the reader will accept for
        its whole remaining life.

        The plan is propagated into the SAME record object the caller
        continues with (B2/B3): the authorization write alone does not make
        the plan durable, because every later journal write — STAGED_VALIDATED,
        ADOPT_INTENT, PROMOTED, the rollback and the abort paths — replaces
        the whole journal leaf.  A plan that is only ever written once is
        durably overwritten with ``None`` by the very next phase write, and
        a crash after that withdraws nothing.
        """
        candidate = dict(record)
        candidate["staged_attachment_state"] = plan
        if _journal_size_ceiling(candidate) > _JOURNAL_MAX_BYTES:
            # Fail closed BEFORE any live mutation and before any journal
            # write: the journal is absent or still at an earlier
            # reconcilable phase, so the refusal can never wedge startup.
            raise BackupError(
                "restore staging publication plan exceeds the restore journal "
                "size budget; the restore is refused before any live mutation"
            )
        record["staged_attachment_state"] = plan
        self._write_journal(record)

    def _withdraw_staged_attachment_state(self, record: dict[str, object]) -> None:
        """Withdraw exactly the payloads and lifecycle artefacts THIS
        transaction durably authorized for publication.

        Attribution is by the recorded plan alone (I5): a planned CAS leaf is
        unlinked only after its content is proven to be the content-addressed
        payload of that digest, and a planned lifecycle leaf only after its
        bytes match the recorded sha256 and size and its identity is the
        machine's own.  A planned leaf that is absent was never published or
        is already withdrawn (both idempotent outcomes).  A planned leaf
        whose content no longer matches is ambiguous evidence: the authority
        is poisoned and nothing is deleted.  Leaves outside the plan — every
        pre-existing object and lifecycle artefact — are never touched.
        """
        plan = record["staged_attachment_state"]
        if plan is None:
            return
        assert isinstance(plan, dict)
        authority = self._authority
        objects_fd = authority._directory_fd("attachments/objects")
        for digest in plan["objects"]:  # type: ignore[union-attr]
            try:
                claim, fd, _identity = self._open_regular(objects_fd, digest)
            except FileNotFoundError:
                continue
            try:
                content_identity, content_sha, _content_size = self._hash_fd(fd)
            finally:
                self._release_fd(claim)
            if (
                content_sha != digest
                or content_identity.uid != os.geteuid()
                or stat.S_IMODE(content_identity.mode) != 0o600
                or content_identity.nlink != 1
            ):
                authority.poison(
                    "restore withdrawal found a planned payload leaf whose "
                    "content is not the attributable content-addressed payload"
                )
                raise BackupUnclassifiedState(
                    "planned restore payload leaf is unattributable for withdrawal"
                )
            os.unlink(digest, dir_fd=objects_fd)
            os.fsync(objects_fd)
        for namespace in ("gc", "staging", "captures"):
            entries = plan[namespace]  # type: ignore[union-attr]
            namespace_fd = authority._directory_fd(f"attachments/{namespace}")
            for leaf, sha, size in entries:
                try:
                    claim, fd, _identity = self._open_regular(namespace_fd, leaf)
                except FileNotFoundError:
                    continue
                try:
                    content_identity, content_sha, content_size = self._hash_fd(fd)
                finally:
                    self._release_fd(claim)
                if (
                    content_sha != sha
                    or content_size != size
                    or int(content_identity.size) != size
                    or content_identity.uid != os.geteuid()
                    or stat.S_IMODE(content_identity.mode) != 0o600
                    or content_identity.nlink != 1
                ):
                    authority.poison(
                        "restore withdrawal found a planned lifecycle leaf "
                        "whose content does not match its recorded identity"
                    )
                    raise BackupUnclassifiedState(
                        "planned restore lifecycle leaf is unattributable "
                        "for withdrawal"
                    )
                os.unlink(leaf, dir_fd=namespace_fd)
                os.fsync(namespace_fd)

    def _sweep_transaction_temp_leaves(self, txid: str) -> None:
        """Remove this transaction's deterministic private temp debris.

        A crash between an O_EXCL temp write and its rename leaves
        ``.bots5-restore-journal-<txid>-<seq>.tmp`` (or the receipt temp)
        behind.  The leaves embed this journal's validated transaction id, so
        a matching leaf is attributable debris of THIS transaction alone and
        its removal restores the database directory inventory; anything else
        is never touched.
        """
        if not _uuid7_text(txid):
            raise BackupUnclassifiedState("restore transaction id is invalid")
        database_fd = self._database_fd()
        journal_temp = re.compile(
            rf"\A\.bots5-restore-journal-{re.escape(txid)}-\d+\.tmp\Z"
        )
        receipt_temp = f".bots5-restore-receipt-{txid}.tmp"
        removed = False
        for name in sorted(self._authority.fresh_directory_inventory("database")):
            if name != receipt_temp and journal_temp.fullmatch(name) is None:
                continue
            claim, _fd, _identity = self._open_regular(database_fd, name)
            self._release_fd(claim)
            os.unlink(name, dir_fd=database_fd)
            removed = True
        if removed:
            os.fsync(database_fd)

    # ------------------------------------------------------------------
    # D-F=F.2 restored-GC precedence (invariant I17): attribution proof and
    # recovery-artefact staging.  The restored durable `deleting` state is
    # authoritative for a matching CAS payload; the deletion itself is
    # performed exclusively by the existing Phase 6 GC recovery machinery
    # (``sqlite.py`` startup recovery) on the next store open — no second GC
    # implementation exists here.  Attribution requires the restored
    # authoritative database state, gc_id, tombstone, digest, expected size
    # and payload identity to be mutually consistent and to pass the existing
    # integrity checks; anything else fails closed and deletes nothing.
    # ------------------------------------------------------------------

    def _candidate_attachment_state(
        self, record: dict[str, object]
    ) -> dict[str, object]:
        """Read the restored candidate's authoritative attachment state."""
        leaf = str(record["candidate_leaf"])
        database_fd = self._database_fd()
        claim, claimed_fd, _ = self._open_regular(database_fd, leaf, flags=os.O_RDWR)
        try:
            vfs = self._open_private_vfs(
                leaf,
                claimed_fd,
                str(record["restore_transaction_id"]),
                # A distinct resource identity: a resource label may be
                # registered only once per authority instance, and the
                # candidate validation battery registers its own.
                resource_label=(
                    f"restore-attachment-state:{record['restore_transaction_id']}"
                ),
            )
        except BaseException:
            self._release_fd(claim)
            raise
        try:
            connection = vfs.connect()
            try:
                state = self._read_attachment_state(connection)
            finally:
                connection.close()
            if vfs.open_count != 0:
                raise BackupUnclassifiedState(
                    "candidate attachment state observation left database files open"
                )
        finally:
            vfs.close()
            self._release_fd(claim)
        return state

    def _read_attachment_state(self, connection) -> dict[str, object]:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        state: dict[str, object] = {
            "capable": "attachment_blobs" in tables,
            "objects": {},
            "deleting_gc": {},
            "staging_operations": {},
            "staging_stage_names": {},
        }
        if not state["capable"]:
            return state

        def merge(digest_hex: object, byte_size: object) -> int:
            if (
                type(digest_hex) is not str
                or not _sha256_text(digest_hex.lower())
                or type(byte_size) is not int
                or byte_size < 0
            ):
                raise BackupUnclassifiedState(
                    "restored attachment state carries a malformed payload identity"
                )
            digest_text = digest_hex.lower()
            size = byte_size
            known = state["objects"].get(digest_text)
            if known is not None and known != size:
                raise BackupUnclassifiedState(
                    "restored attachment state is self-contradictory about a payload size"
                )
            state["objects"][digest_text] = size
            return size

        rows = connection.execute(
            "SELECT hex(digest), byte_size, state, operation_id, stage_name, gc_id "
            "FROM attachment_blobs ORDER BY digest"
        ).fetchall()
        for digest_hex, byte_size, blob_state, operation_id, stage_name, gc_id in rows:
            size = merge(digest_hex, byte_size)
            if blob_state == "deleting":
                if type(gc_id) is not str or not _uuid7_text(gc_id):
                    raise BackupUnclassifiedState(
                        "restored deleting state has no attributable gc identity"
                    )
                state["deleting_gc"][gc_id] = (str(digest_hex).lower(), size)
            elif blob_state == "staging":
                if (
                    type(operation_id) is not str
                    or not _uuid7_text(operation_id)
                    or stage_name != operation_id
                ):
                    raise BackupUnclassifiedState(
                        "restored staging state has no attributable operation identity"
                    )
                state["staging_operations"][operation_id] = (
                    str(digest_hex).lower(),
                    size,
                )
                state["staging_stage_names"][stage_name] = (
                    str(digest_hex).lower(),
                    size,
                )
            elif blob_state != "ready":
                raise BackupUnclassifiedState(
                    "restored attachment state carries an unknown blob state"
                )
        if (
            "archive_import_payload_reservations" in tables
            and "archive_import_operations" in tables
        ):
            for digest_hex, size in connection.execute(
                "SELECT hex(r.digest), r.size FROM archive_import_payload_reservations r "
                "JOIN archive_import_operations o ON o.id=r.operation_id "
                "WHERE r.publication_state='READY' AND o.state IN ('STAGING','COMMITTING')"
            ).fetchall():
                merge(digest_hex, size)
        return state

    def _extract_package_entry(
        self,
        package_path: Path,
        entry_path: str,
        dir_fd: int,
        leaf: str,
        size: int,
        sha: str,
        label: str,
    ) -> None:
        """Extract one package entry into a private directory, verified."""
        with zipfile.ZipFile(package_path) as package:
            try:
                info = package.getinfo(entry_path)
            except KeyError as exc:
                raise BackupArchiveInvalid(
                    "restore package is missing a declared entry"
                ) from exc
            fd = os.open(
                leaf,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
            claim = self._own_fd(label, fd)
            digest = hashlib.sha256()
            copied = 0
            try:
                with package.open(info, "r") as source:
                    while block := source.read(_CHUNK):
                        digest.update(block)
                        copied += len(block)
                        self._write_all_fd(fd, block)
                os.fsync(fd)
            finally:
                self._release_fd(claim)
        if copied != size or digest.hexdigest() != sha:
            raise BackupArchiveInvalid(
                "extracted package entry does not match its manifest identity"
            )

    def _observe_target_attachment_namespaces(
        self,
    ) -> dict[str, frozenset[str]]:
        """Baseline-checked fresh inventory of the target attachment namespaces.

        Taken BEFORE the restore writes anything: the ``restore-<txid>``
        scratch subdirectory created by ``_stage_payloads`` would otherwise
        change ``attachments/staging``'s link count and fail the baseline
        comparison.  These observations are therefore the proof of the
        target's pre-existing attachment state.
        """
        authority = self._authority
        return {
            "objects": frozenset(
                authority.fresh_directory_inventory("attachments/objects")
            ),
            "gc": frozenset(authority.fresh_directory_inventory("attachments/gc")),
            "staging": frozenset(
                authority.fresh_directory_inventory("attachments/staging")
            ),
            "captures": frozenset(
                authority.fresh_directory_inventory("attachments/captures")
            ),
        }

    def _attribute_and_stage_recovery_state(
        self,
        record: dict[str, object],
        state: dict[str, object],
        observed: dict[str, frozenset[str]],
        package_path: Path,
        payloads: dict[str, int],
        artifacts: dict[tuple[str, str], tuple[int, str]],
    ) -> None:
        """Prove restored-state/target/package attribution; stage artefacts.

        Fail-closed rules (I5, I17, D-F=F.2):

        - every canonical object in the target ``attachments/objects`` must be
          attributed by the restored authoritative database (ready, staging,
          deleting rows and live archive-import reservations) and must pass
          the existing content integrity checks; otherwise the authority is
          poisoned and nothing is deleted;
        - every target lifecycle leaf (``attachments/gc``, ``staging``,
          ``captures``) must be attributed by the restored database;
          otherwise the authority is poisoned and nothing is deleted;
        - every package recovery artefact must be attributable to the
          restored database and its bytes must be a valid tombstone or the
          matching payload of the restored state; otherwise the restore is
          refused with the live installation untouched.

        Package payloads already pass through ``_stage_payloads`` /
        ``_verify_object_leaf``; this pass covers exactly the rest.
        """
        authority = self._authority
        txid = str(record["restore_transaction_id"])
        capable = bool(state["capable"])
        objects_fd = authority._directory_fd("attachments/objects")
        namespace_fd = {
            "gc": authority._directory_fd("attachments/gc"),
            "staging": authority._directory_fd("attachments/staging"),
            "captures": authority._directory_fd("attachments/captures"),
        }
        private_staging_leaf = f"restore-{txid}"

        # 1. Objects: attribution plus the existing integrity checks.  The
        # observed set is the target's pre-existing state; package payloads
        # staged by ``_stage_payloads`` complete it (deduplicated payloads
        # were already part of it) and are verified by the payload path.
        # This is the pre-adoption proof for every payload the restored
        # durable `deleting` state may later delete through Phase 6 GC
        # recovery: a content mismatch is an I5 ambiguity and must never be
        # deleted.
        attributable: dict[str, int] = state["objects"]  # type: ignore[assignment]
        post_payload_objects = set(observed["objects"]) | set(payloads)
        for leaf in sorted(post_payload_objects):
            if leaf in payloads:
                continue  # verified or renamed into place by the payload path
            if not capable or leaf not in attributable:
                authority.poison(
                    "restore target carries a canonical payload object the restored "
                    "authoritative database does not attribute"
                )
                raise BackupUnclassifiedState(
                    "unattributable canonical payload object in the restore target"
                )
            self._verify_object_leaf(objects_fd, leaf, attributable[leaf])

        # 2. Lifecycle namespaces: attribution only (never deletion).
        for leaf in sorted(observed["gc"]):
            if capable and leaf in state["deleting_gc"]:  # type: ignore[operator]
                continue
            authority.poison(
                "restore target carries an unattributable GC lifecycle artefact"
            )
            raise BackupUnclassifiedState(
                "unattributable GC lifecycle artefact in the restore target"
            )
        for leaf in sorted(observed["staging"] - {private_staging_leaf}):
            if capable and leaf in state["staging_stage_names"]:  # type: ignore[operator]
                continue
            authority.poison(
                "restore target carries an unattributable staging lifecycle artefact"
            )
            raise BackupUnclassifiedState(
                "unattributable staging lifecycle artefact in the restore target"
            )
        for leaf in sorted(observed["captures"]):
            if capable and leaf in state["staging_operations"]:  # type: ignore[operator]
                continue
            authority.poison(
                "restore target carries an unattributable capture lifecycle artefact"
            )
            raise BackupUnclassifiedState(
                "unattributable capture lifecycle artefact in the restore target"
            )

        # 3. Package recovery artefacts: attribute, extract, prove, publish.
        if not artifacts:
            return
        staging_fd = authority._directory_fd("attachments/staging")
        dir_fd = os.open(
            private_staging_leaf,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=staging_fd,
        )
        dir_claim = self._own_fd(f"restore-artifacts:{private_staging_leaf}", dir_fd)
        try:
            for (namespace, leaf), (size, sha) in sorted(artifacts.items()):
                if namespace == "gc":
                    row = state["deleting_gc"].get(leaf)  # type: ignore[union-attr]
                elif namespace == "staging":
                    row = state["staging_stage_names"].get(leaf)  # type: ignore[union-attr]
                else:
                    row = state["staging_operations"].get(leaf)  # type: ignore[union-attr]
                if not capable or row is None:
                    # A package artefact the restored authoritative database
                    # does not attribute is not one mechanically attributable
                    # recovery state: fail closed, no deletion, no staging.
                    raise BackupUnclassifiedState(
                        "package recovery artefact is not attributable to the "
                        "restored authoritative database"
                    )
                digest_hex, byte_size = row
                self._extract_package_entry(
                    package_path,
                    f"recovery-artifacts/attachments/{namespace}/{leaf}",
                    dir_fd,
                    leaf,
                    size,
                    sha,
                    f"restore-artifact:{namespace}:{leaf}",
                )
                content_identity, content_sha, content_size = self._hash_leaf(
                    dir_fd, leaf
                )
                if content_sha != sha or content_size != size:
                    raise BackupUnclassifiedState(
                        "package recovery artefact does not match its manifest identity"
                    )
                if namespace == "gc":
                    tombstone = _AttachmentFS.tombstone_bytes(
                        leaf, bytes.fromhex(digest_hex)
                    )
                    is_tombstone = content_size == len(tombstone) and (
                        content_sha == hashlib.sha256(tombstone).hexdigest()
                    )
                    if not is_tombstone and (
                        content_sha != digest_hex or content_size != byte_size
                    ):
                        raise BackupUnclassifiedState(
                            "GC recovery artefact is neither an attributable tombstone "
                            "nor the attributable payload of the restored deleting state"
                        )
                elif content_sha != digest_hex or content_size != byte_size:
                    raise BackupUnclassifiedState(
                        "staging recovery artefact does not match the restored "
                        "staging state"
                    )
                existing = {
                    "gc": set(observed["gc"]),
                    "staging": set(observed["staging"]),
                    "captures": set(observed["captures"]),
                }[namespace]
                if leaf in existing:
                    existing_identity, existing_sha, existing_size = self._hash_leaf(
                        namespace_fd[namespace], leaf
                    )
                    if existing_sha != content_sha or existing_size != content_size:
                        authority.poison(
                            "existing lifecycle artefact contradicts the restore package"
                        )
                        raise BackupUnclassifiedState(
                            "existing lifecycle artefact contradicts the restore package"
                        )
                    os.unlink(leaf, dir_fd=dir_fd)
                    os.fsync(dir_fd)
                else:
                    _rename_noreplace(dir_fd, leaf, namespace_fd[namespace], leaf)
                    os.fsync(namespace_fd[namespace])
                    os.fsync(dir_fd)
                    existing.add(leaf)
        finally:
            self._release_fd(dir_claim)

    def _purge_restore_staging(self, txid: str) -> None:
        authority = self._authority
        staging_fd = authority._directory_fd("attachments/staging")
        leaf = f"restore-{txid}"
        try:
            dir_fd = os.open(
                leaf,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=staging_fd,
            )
        except FileNotFoundError:
            return
        dir_claim = self._own_fd(f"payload-staging-purge:{leaf}", dir_fd)
        try:
            for name in sorted(os.listdir(dir_fd)):
                if _SHA256.fullmatch(name) is None and not _uuid7_text(name):
                    # Payload scratch is content-addressed; recovery-artefact
                    # scratch is uuid7-addressed.  Anything else is
                    # unattributable residue and is never silently removed.
                    raise BackupUnclassifiedState(
                        "restore payload staging contains unattributable residue"
                    )
                claim, fd, _ = self._open_regular(dir_fd, name)
                self._release_fd(claim)
                os.unlink(name, dir_fd=dir_fd)
            os.fsync(dir_fd)
        finally:
            self._release_fd(dir_claim)
        os.rmdir(leaf, dir_fd=staging_fd)
        os.fsync(staging_fd)

    def _discard_candidate(self, record: dict[str, object]) -> None:
        leaf = str(record["candidate_leaf"])
        database_fd = self._database_fd()
        try:
            claim, fd, _ = self._open_regular(database_fd, leaf)
        except FileNotFoundError:
            return
        self._release_fd(claim)
        os.unlink(leaf, dir_fd=database_fd)
        os.fsync(database_fd)

    def _validation_battery(self, connection, expected_revision: str) -> None:
        from bots5.infrastructure.persistence.migration_runner import _MIGRATION_CHAIN

        if expected_revision not in _MIGRATION_CHAIN:
            raise BackupUnsupported(
                "restore revision is outside the supported migration chain"
            )
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("restore candidate integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise BackupError("restore candidate foreign keys are inconsistent")
        mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
        if mode != "delete":
            raise BackupError("restore candidate journal mode is not delete")
        connection.execute("PRAGMA synchronous=FULL")
        if int(connection.execute("PRAGMA synchronous").fetchone()[0]) != 2:
            raise BackupError("restore candidate synchronous mode is not FULL")
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        if len(rows) != 1 or str(rows[0][0]) != expected_revision:
            raise BackupError("restore candidate revision is inconsistent")

    def _open_private_vfs(
        self,
        leaf: str,
        claimed_fd: int,
        txid: str,
        *,
        resource_label: str | None = None,
    ) -> RootedSQLiteVfs:
        authority = self._authority
        try:
            return RootedSQLiteVfs(
                database_dir_fd=self._database_fd(),
                main_claim_fd=claimed_fd,
                temp_dir_fd=authority._directory_fd("database/temp"),
                mount_id=authority._root_identity.mount_id,
                main_leaf=leaf,
                journal_leaf=leaf + "-journal",
                intake_wal=False,
                authority=authority,
                resource_label=(
                    resource_label or f"restore-candidate-vfs:{txid}"
                ),
            )
        except RootedVfsRegistrationCloseUnknown as exc:
            raise BackupUnclassifiedState(
                "restore candidate VFS registration cleanup incomplete"
            ) from exc

    def _validate_candidate(self, record: dict[str, object]) -> None:
        leaf = str(record["candidate_leaf"])
        database_fd = self._database_fd()
        claim, claimed_fd, _ = self._open_regular(database_fd, leaf, flags=os.O_RDWR)
        try:
            vfs = self._open_private_vfs(
                leaf, claimed_fd, str(record["restore_transaction_id"])
            )
        except BaseException:
            self._release_fd(claim)
            raise
        try:
            connection = vfs.connect()
            try:
                self._validation_battery(connection, str(record["target_db_revision"]))
            finally:
                connection.close()
            if vfs.open_count != 0:
                raise BackupUnclassifiedState(
                    "candidate validation left database files open"
                )
        finally:
            vfs.close()
            self._release_fd(claim)

    def _reverify_payload_bijection(
        self, record: dict[str, object], payloads: dict[str, int]
    ) -> None:
        """DEFECT-8: re-prove the staged payload set immediately before adoption."""
        objects_fd = self._authority._directory_fd("attachments/objects")
        for digest, size in sorted(payloads.items()):
            claim, fd, identity = self._open_regular(objects_fd, digest)
            try:
                _, sha, byte_size = self._hash_fd(fd)
            finally:
                self._release_fd(claim)
            if sha != digest or byte_size != size or identity.size != size:
                self._authority.poison(
                    "required restore payload integrity failure before adoption"
                )
                raise BackupUnclassifiedState(
                    "required restore payload does not match its durable identity"
                )

    # ------------------------------------------------------------------
    # Adoption, post-adoption validation, rollback (machine steps 5-8).
    # ------------------------------------------------------------------

    def _validate_promoted(self, record: dict[str, object]) -> None:
        _fault("post-adoption-validation")
        authority = self._authority
        authority._claim_database()
        vfs = authority._open_rooted_vfs()
        try:
            connection = vfs.connect()
            try:
                self._validation_battery(
                    connection, str(record["target_db_revision"])
                )
            finally:
                connection.close()
            if vfs.open_count != 0:
                raise BackupUnclassifiedState(
                    "promoted validation left database files open"
                )
        finally:
            authority._close_database_vfs()
        authority.database_durability_fence()

    def _refuse_displaced_sidecar_residue(self, record: dict[str, object]) -> None:
        """DEFECT-4: a displaced database with sidecar residue is not promotable."""
        database_fd = self._database_fd()
        displaced = str(record["displaced_leaf"])
        names = set(self._authority.fresh_directory_inventory("database"))
        residue = sorted(
            name for name in names if any(name == f"{displaced}{suffix}" for suffix in _SIDECARS)
        )
        if residue:
            self._authority.poison(
                "displaced rollback source carries SQLite sidecar residue"
            )
            raise BackupUnclassifiedState(
                "displaced rollback source carries SQLite sidecar residue: "
                + ", ".join(residue)
            )

    def _hash_canonical_main(self) -> tuple[FileIdentity, str, int]:
        main_fd = self._authority._claim_database()
        return self._hash_fd(main_fd)

    def _revalidate_rollback_target(self, record: dict[str, object]) -> None:
        preservation = record["preservation_record"]
        assert preservation is not None
        try:
            identity, sha, size = self._hash_canonical_main()
        except FileNotFoundError as exc:
            raise BackupUnclassifiedState(
                "rollback target is missing after the re-exchange"
            ) from exc
        if (
            sha != preservation["sha256"]
            or size != preservation["bytes"]
            or identity.size != preservation["bytes"]
        ):
            self._authority.poison(
                "rolled-back database does not match the preserved installation"
            )
            raise BackupUnclassifiedState(
                "rolled-back database does not match the preserved installation"
            )

    def _rollback(self, record: dict[str, object], cause: BaseException) -> NoReturn:
        try:
            record = self._advance(record, "RESTORE_ROLLBACK_INTENT")
            self._write_journal(record)
            self._refuse_displaced_sidecar_residue(record)
            try:
                self._authority._restore_exchange_database_claim(
                    str(record["displaced_leaf"])
                )
            except OSError as exc:
                # A raised exchange means the re-exchange did not occur
                # (fail-loud primitive); there is no attributable outcome.
                self._fail_closed(
                    f"rollback re-exchange did not occur: {exc}", record
                )
            _fault("after-rollback-exchange")
            self._revalidate_rollback_target(record)
            # The transaction reached a known-safe terminal state: its private
            # payload staging is scratch (cancel semantics) and is discarded;
            # the displaced candidate leaf and the preservation artefact are
            # kept as attributable evidence (I14, D-A=A.2).
            self._purge_restore_staging(str(record["restore_transaction_id"]))
            record = self._advance(record, "ROLLED_BACK")
            self._write_journal(record)
        except BackupUnclassifiedState as exc:
            # D-E=E.1: a missing, mismatched or unattributable rollback source
            # never guesses and never silently retains the candidate.  Record
            # the durable failed-closed marker with evidence preserved, poison
            # the authority, and stop forward mutation (I5, I16).
            self._fail_closed(f"restore rollback failed closed: {exc}", record)
        except BaseException as exc:
            self._fail_closed(f"restore rollback failed: {exc}", record)
        raise BackupError(
            "restore rolled back to the preserved installation; restart required"
        ) from cause

    def _fail_closed(self, message: str, record: dict[str, object]) -> NoReturn:
        """RESTORE_FAILED_CLOSED: durable marker, evidence kept, poison, raise."""
        try:
            record = self._advance(record, "RESTORE_FAILED_CLOSED")
            self._write_journal(record)
        except BaseException:
            # Evidence preservation outranks marker durability; the poison
            # below still closes forward mutation (I5).
            pass
        self._authority.poison(message)
        raise BackupUnclassifiedState(message)

    # ------------------------------------------------------------------
    # Receipt (spec section 8) and commit.
    # ------------------------------------------------------------------

    def _build_receipt(
        self, record: dict[str, object], *, outcome: str = "RESTORED"
    ) -> dict[str, object]:
        from bots5.infrastructure.persistence.migration_runner import _HEAD

        source_revision = str(record["target_db_revision"])
        return {
            "backup_id": record["backup_id"],
            "package_sha256": record["package_sha256"],
            "source_revision": source_revision,
            "target_revision_at_commit": source_revision,
            "post_adoption_migration": (
                "required" if source_revision < _HEAD else "not_required"
            ),
            "target_revision_after_migration": None,
            "migration_recovery_reference": None,
            "transaction_id": record["restore_transaction_id"],
            "outcome": outcome,
        }

    def _write_receipt(self, receipt: dict[str, object], txid: str) -> None:
        if set(receipt) != set(_RECEIPT_FIELDS):
            raise BackupError("restore receipt schema is not the closed field set")
        database_fd = self._database_fd()
        temp_leaf = f".bots5-restore-receipt-{txid}.tmp"
        self._atomic_write_private(
            database_fd, temp_leaf, _RESTORE_RECEIPT_LEAF, canonical_backup_json(receipt)
        )

    def _read_receipt(self) -> dict[str, object] | None:
        database_fd = self._database_fd()
        try:
            claim, fd, _ = self._open_regular(database_fd, _RESTORE_RECEIPT_LEAF)
        except FileNotFoundError:
            return None
        try:
            raw = os.read(fd, _JOURNAL_MAX_BYTES).decode("utf-8")
        finally:
            self._release_fd(claim)
        try:
            receipt = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupUnclassifiedState(
                "restore receipt is torn or non-canonical and is not trusted"
            ) from exc
        if not isinstance(receipt, dict) or set(receipt) != set(_RECEIPT_FIELDS):
            raise BackupUnclassifiedState("restore receipt is not the closed schema")
        self._validate_receipt_invariants(receipt)
        return receipt

    def _validate_receipt_invariants(self, receipt: dict[str, object]) -> None:
        """Fail closed on any receipt whose recorded state is self-contradictory.

        The receipt is the durable record of the adopt-then-migrate handoff
        (spec section 8, RESTORE_STATE_MACHINE.md section 3.1): a terminal
        ``failed_rolled_back`` state must carry the migration recovery
        reference and no post-migration revision, a ``completed`` state must
        carry the post-migration revision and no recovery reference, and a
        pending state must carry neither.  Ambiguous receipts are never
        partially trusted.
        """
        from bots5.infrastructure.persistence.migration_runner import _MIGRATION_CHAIN

        state = receipt["post_adoption_migration"]
        if state not in _RECEIPT_MIGRATION_STATES:
            raise BackupUnclassifiedState(
                "restore receipt migration state is unattributable"
            )
        reference = receipt["migration_recovery_reference"]
        if state == "failed_rolled_back":
            if not _uuid7_text(reference):
                raise BackupUnclassifiedState(
                    "restore receipt records a rolled-back post-adoption "
                    "migration without an attributable migration recovery "
                    "reference"
                )
        elif reference is not None:
            raise BackupUnclassifiedState(
                "restore receipt records a migration recovery reference "
                "without a rolled-back post-adoption migration"
            )
        after = receipt["target_revision_after_migration"]
        if state == "completed":
            if type(after) is not str or after not in _MIGRATION_CHAIN:
                raise BackupUnclassifiedState(
                    "restore receipt finalisation revision is unattributable"
                )
        elif after is not None:
            raise BackupUnclassifiedState(
                "restore receipt records a post-migration revision before "
                "finalisation"
            )

    # ------------------------------------------------------------------
    # Forward execution (machine section 3).
    # ------------------------------------------------------------------

    def restore(
        self,
        package_path: Path | str,
        *,
        expected_backup_id: str | None = None,
    ) -> dict[str, object]:
        """Execute one whole-installation restore transaction to RESTORE_COMMITTED."""
        with self._authority.transition():
            receipt = self._restore(Path(package_path), expected_backup_id)
        self._refresh_staging_baseline()
        return receipt

    def _restore(
        self, package_path: Path, expected_backup_id: str | None
    ) -> dict[str, object]:
        # VERIFYING: artifact-only, no journal, live state untouched (I1).
        from bots5.infrastructure.backup_package import BackupZipPackageAdapter

        receipt = BackupZipPackageAdapter().verify(
            package_path, expected_backup_id=expected_backup_id
        )
        backup_id = receipt.backup_id
        package_sha256 = receipt.artifact_sha256
        source_revision = receipt.source_db_migration_revision
        from bots5.infrastructure.persistence.migration_runner import _MIGRATION_CHAIN

        if source_revision not in _MIGRATION_CHAIN:
            raise BackupUnsupported(
                "backup revision is outside the supported migration chain"
            )
        # IDLE gate (I12): an existing journal must be reconciled first.
        try:
            existing = self._read_journal()
        except BackupUnclassifiedState:
            self._authority.poison("torn restore journal discovered before a new restore")
            raise
        if existing is not None:
            raise BackupError(
                "an interrupted restore journal is present; run reconciliation first"
            )
        txid = str(uuid7())
        package_text = os.path.abspath(os.fspath(package_path))
        record = self._base_record(
            txid=txid,
            backup_id=backup_id,
            package_path=package_text,
            package_sha256=package_sha256,
            target_db_revision=source_revision,
        )
        lock_leaf = self._lock_leaf(txid)
        lock_claim = self._open_transaction_lock(txid)
        try:
            return self._execute_transaction(record, package_path, lock_claim, lock_leaf)
        finally:
            # Non-terminal exits keep the journal for restart attribution but
            # never keep the flock: an abandoned transaction is attributed as
            # crashed by the unheld lock plus the recorded phase (I11).
            try:
                self._release_transaction_lock(lock_claim, lock_leaf, unlink=False)
            except BaseException:
                self._authority.poison(
                    "restore transaction lock release outcome is uncertain"
                )
                raise

    def _execute_transaction(
        self,
        record: dict[str, object],
        package_path: Path,
        lock_claim,
        lock_leaf: str,
    ) -> dict[str, object]:
        # PRESERVING: journal the phase before touching anything (W2 window).
        self._write_journal(record)
        self._quiesce()
        record["preservation_record"] = self._preserve(record)
        record = self._advance(record, "PRESERVED")
        self._write_journal(record)

        # STAGING.
        record = self._advance(record, "STAGING")
        self._write_journal(record)
        candidate_identity, payloads = self._stage_package(
            record, package_path, str(record["package_sha256"])
        )
        self._validate_candidate(record)
        self._reverify_payload_bijection(record, payloads)
        record = self._advance(record, "STAGED_VALIDATED")
        self._write_journal(record)

        # ADOPT_INTENT: the uncancellable, fsynced boundary.
        self._quiesce()
        self._prove_rollback_source(record)
        record = self._advance(record, "ADOPT_INTENT")
        self._write_journal(record)

        # PROMOTED: exactly one exchange; a raised OSError means the exchange
        # did not occur and is never treated as success (I6/DEFECT-1).
        _fault("before-adopt-exchange")
        try:
            promoted_identity = self._authority._restore_exchange_database_claim(
                str(record["candidate_leaf"])
            )
        except OSError as exc:
            self._fail_closed(
                f"restore exchange did not occur or its outcome is unprovable: {exc}",
                record,
            )
        _fault("after-adopt-exchange")
        if (
            promoted_identity.key != candidate_identity.key
            or promoted_identity.size != candidate_identity.size
        ):
            self._fail_closed(
                "promoted database identity does not match the staged candidate",
                record,
            )
        record = self._advance(record, "PROMOTED")
        self._write_journal(record)

        # POST_ADOPTION_VALIDATED, with the automatic rollback machine.
        try:
            self._validate_promoted(record)
        except Exception as validation_error:
            self._rollback(record, validation_error)
        record = self._advance(record, "POST_ADOPTION_VALIDATED")
        self._write_journal(record)

        # CLEANUP_PENDING: private staging only; the preserved installation is
        # never removed here (D-A=A.2).
        record = self._advance(record, "CLEANUP_PENDING")
        self._write_journal(record)
        self._purge_restore_staging(str(record["restore_transaction_id"]))

        # RESTORE_COMMITTED: receipt, terminal journal, journal unlink.
        receipt = self._build_receipt(record)
        self._write_receipt(receipt, str(record["restore_transaction_id"]))
        record = self._advance(record, "RESTORE_COMMITTED")
        self._write_journal(record)
        self._unlink_journal()
        self._release_transaction_lock(lock_claim, lock_leaf, unlink=True)
        return receipt

    # ------------------------------------------------------------------
    # Restart reconciliation (machine section 4, crash windows W2-W10).
    # ------------------------------------------------------------------

    def reconcile(self) -> dict[str, object] | None:
        """Attribute and converge one interrupted restore transaction.

        Runs strictly from the durable journal phase plus recorded identities
        and lock state; never from assumption (I11).  Returns ``None`` when the
        data root is idle.  Normal startup may proceed only after a
        reconciliation outcome of ``"aborted"`` or ``"completed"``.
        """
        with self._authority.transition():
            summary = self._reconcile()
        self._refresh_staging_baseline()
        return summary

    def _refresh_staging_baseline(self) -> None:
        """Re-baseline ``attachments/staging`` after authorized staging work.

        The restore machine is the only writer of the ``restore-<txid>``
        staging child: it creates it while staging payloads and removes it
        when scratch is discarded.  Creating and removing that subdirectory
        changes the fixed descendant's link count, so the acquire-time
        directory baseline would classify the post-reconciliation topology
        as a foreign change and fail every later fresh inventory on this
        same authority instance.  Re-derive the baseline from the
        authority-held descriptor, exactly like the acquire path persists
        the post-bootstrap topology after its own authorized mutations.
        """
        authority = self._authority
        staging_fd = authority._directory_fd("attachments/staging")
        authority._directory_baselines["attachments/staging"] = (
            DirectoryIdentityBaseline.from_identity(
                _check_directory(staging_fd, mount_id=authority._root_identity.mount_id)
            )
        )

    def _reconcile(self) -> dict[str, object] | None:
        try:
            existing = self._read_journal()
        except BackupUnclassifiedState:
            # W10: preserve the bytes, poison the authority, never guess.
            self._authority.poison("restore journal is torn or non-canonical")
            raise
        if existing is None:
            self._sweep_orphan_locks()
            return None
        record, _raw = existing
        txid = str(record["restore_transaction_id"])
        lock_leaf = self._lock_leaf(txid)
        lock_claim = self._open_transaction_lock(txid)
        try:
            return self._reconcile_transaction(record, txid, lock_claim, lock_leaf)
        finally:
            try:
                self._release_transaction_lock(lock_claim, lock_leaf, unlink=False)
            except BaseException:
                self._authority.poison(
                    "restore transaction lock release outcome is uncertain"
                )
                raise

    def _reconcile_transaction(
        self,
        record: dict[str, object],
        txid: str,
        lock_claim,
        lock_leaf: str,
    ) -> dict[str, object]:
        phase = str(record["phase"])
        if phase == "RESTORE_COMMITTED":
            # Crash between the terminal journal write and its unlink: finish.
            receipt = self._read_receipt()
            self._unlink_journal()
            self._release_transaction_lock(lock_claim, lock_leaf, unlink=True)
            return {
                "journal_phase": phase,
                "action": "completed",
                "restart_required": False,
                "receipt": receipt,
                "transaction_id": txid,
            }
        if phase == "RESTORE_FAILED_CLOSED":
            self._authority.poison(
                "restore journal is in the failed-closed state; human inspection required"
            )
            raise BackupUnclassifiedState(
                "restore journal is in the failed-closed state; human inspection required"
            )
        if phase in _SCRATCH_PHASES:
            self._discard_scratch(record)
            return {
                "journal_phase": phase,
                "action": "aborted",
                "restart_required": False,
                "receipt": None,
                "transaction_id": txid,
            }
        # A journal beyond the scratch phases with a null preservation record
        # is a D-D=D.2 destructive transaction: no rollback source exists and
        # the rollback machine was never armed for it.  Its convergence rules
        # attribute by the recorded candidate content digest instead of a
        # preservation identity.
        destructive = record["preservation_record"] is None
        if phase == "ADOPT_INTENT":
            if destructive:
                return self._reconcile_destructive_adopt_intent(
                    record, txid, lock_claim, lock_leaf
                )
            return self._reconcile_adopt_intent(
                record, txid, lock_claim, lock_leaf
            )
        if phase == "PROMOTED":
            if destructive:
                return self._reconcile_destructive_promoted(
                    record, txid, lock_claim, lock_leaf
                )
            return self._reconcile_promoted(record, txid, lock_claim, lock_leaf)
        if phase in {"POST_ADOPTION_VALIDATED", "CLEANUP_PENDING"}:
            if destructive:
                return self._reconcile_destructive_forward(
                    record, txid, lock_claim, lock_leaf
                )
            return self._reconcile_forward(record, txid, lock_claim, lock_leaf)
        if phase == "RESTORE_ROLLBACK_INTENT":
            if destructive:
                self._fail_closed(
                    "destructive restore journal carries an impossible rollback "
                    "phase; the evidence is unattributable",
                    record,
                )
            return self._reconcile_rollback_intent(record, txid)
        if phase == "ROLLED_BACK":
            if destructive:
                self._fail_closed(
                    "destructive restore journal carries an impossible rolled-back "
                    "phase; the evidence is unattributable",
                    record,
                )
            return self._reconcile_rolled_back(record, txid, lock_claim, lock_leaf)
        raise BackupUnclassifiedState(
            f"restore journal phase is not reconcilable: {phase}"
        )

    def _discard_scratch(self, record: dict[str, object]) -> None:
        txid = str(record["restore_transaction_id"])
        if record["phase"] == "PRESERVING":
            # W2: the partial artefact is unrecorded scratch; discard it.
            retained_fd = self._authority._retained_installations_dir_capability
            leaf = self._preservation_leaf(record)
            try:
                claim, fd, _ = self._open_regular(retained_fd, leaf)
            except FileNotFoundError:
                pass
            else:
                self._release_fd(claim)
                os.unlink(leaf, dir_fd=retained_fd)
                os.fsync(retained_fd)
        # Withdraw the payloads and lifecycle artefacts this transaction
        # published into the live namespaces before it aborted (B2/B3).
        self._withdraw_staged_attachment_state(record)
        self._discard_candidate(record)
        self._purge_restore_staging(txid)
        self._sweep_transaction_temp_leaves(txid)
        self._unlink_journal()

    def _package_database_digest(self, record: dict[str, object]) -> str | None:
        """Candidate content digest from the recorded package, when readable."""
        package_path = Path(str(record["package_path"]))
        try:
            observed = self._hash_package_file(package_path)
        except (OSError, ValueError):
            return None
        if observed != record["package_sha256"]:
            return None
        manifest = self._package_manifest(package_path)
        capture = manifest["capture"]
        digest = str(capture["source_database_sha256"])
        return digest if _sha256_text(digest) else None

    def _live_matches_preservation(self, record: dict[str, object]) -> bool:
        preservation = record["preservation_record"]
        if preservation is None:
            # Destructive (D-D=D.2) transaction: no preservation record was
            # ever established, so nothing can match it.
            return False
        try:
            _, sha, size = self._hash_canonical_main()
        except (FileNotFoundError, BackupUnclassifiedState):
            return False
        return sha == preservation["sha256"] and size == preservation["bytes"]

    def _live_matches_digest(self, digest: str | None) -> bool:
        if digest is None:
            return False
        try:
            _, sha, _size = self._hash_canonical_main()
        except (FileNotFoundError, BackupUnclassifiedState):
            return False
        return sha == digest

    def _displaced_matches_preservation(self, record: dict[str, object]) -> bool:
        preservation = record["preservation_record"]
        assert preservation is not None
        database_fd = self._database_fd()
        try:
            _, sha, size = self._hash_leaf(database_fd, str(record["displaced_leaf"]))
        except FileNotFoundError:
            return False
        return sha == preservation["sha256"] and size == preservation["bytes"]

    def _reconcile_adopt_intent(
        self, record: dict[str, object], txid: str, lock_claim, lock_leaf: str
    ) -> dict[str, object]:
        # W4/W5: attribute by recorded content identity, never by assumption.
        if self._live_matches_preservation(record):
            # No exchange occurred; abort cleanly.  The payloads and lifecycle
            # artefacts staged before the abort are withdrawn by recorded
            # plan, returning the live installation to its pre-restore state.
            self._withdraw_staged_attachment_state(record)
            self._discard_candidate(record)
            self._purge_restore_staging(txid)
            self._sweep_transaction_temp_leaves(txid)
            self._unlink_journal()
            self._release_transaction_lock(lock_claim, lock_leaf, unlink=True)
            return {
                "journal_phase": "ADOPT_INTENT",
                "action": "aborted",
                "restart_required": False,
                "receipt": None,
                "transaction_id": txid,
            }
        candidate_digest = self._package_database_digest(record)
        if self._live_matches_digest(candidate_digest) and self._displaced_matches_preservation(
            record
        ):
            # W5: the kernel made the exchange atomic; converge forward.
            record = self._advance(record, "PROMOTED")
            self._write_journal(record)
            return self._reconcile_forward(record, txid, lock_claim, lock_leaf)
        self._fail_closed(
            "interrupted adoption cannot be attributed by recorded identity", record
        )

    def _reconcile_promoted(
        self, record: dict[str, object], txid: str, lock_claim, lock_leaf: str
    ) -> dict[str, object]:
        candidate_digest = self._package_database_digest(record)
        if self._live_matches_digest(candidate_digest):
            return self._reconcile_forward(record, txid, lock_claim, lock_leaf)
        if self._live_matches_preservation(record):
            self._fail_closed(
                "promoted journal contradicts the live database identity", record
            )
        self._fail_closed(
            "interrupted promotion cannot be attributed by recorded identity", record
        )

    def _reconcile_forward(
        self, record: dict[str, object], txid: str, lock_claim, lock_leaf: str
    ) -> dict[str, object]:
        """W6 pass / W8: validate, clean private staging, commit."""
        entry_phase = str(record["phase"])
        if entry_phase == "PROMOTED":
            try:
                self._validate_promoted(record)
            except Exception as validation_error:
                self._rollback(record, validation_error)
            record = self._advance(record, "POST_ADOPTION_VALIDATED")
            self._write_journal(record)
        if str(record["phase"]) == "POST_ADOPTION_VALIDATED":
            record = self._advance(record, "CLEANUP_PENDING")
            self._write_journal(record)
        self._purge_restore_staging(txid)
        receipt = self._build_receipt(record)
        self._write_receipt(receipt, txid)
        record = self._advance(record, "RESTORE_COMMITTED")
        self._write_journal(record)
        self._unlink_journal()
        self._release_transaction_lock(lock_claim, lock_leaf, unlink=True)
        return {
            "journal_phase": entry_phase,
            "action": "completed",
            "restart_required": False,
            "receipt": receipt,
            "transaction_id": txid,
        }

    def _reconcile_rollback_intent(self, record: dict[str, object], txid: str) -> dict[str, object]:
        # W7: complete or repeat the rollback deterministically.
        if self._live_matches_preservation(record):
            self._revalidate_rollback_target(record)
        else:
            candidate_digest = self._package_database_digest(record)
            if not self._live_matches_digest(candidate_digest):
                self._fail_closed(
                    "interrupted rollback cannot be attributed by recorded identity",
                    record,
                )
            try:
                self._refuse_displaced_sidecar_residue(record)
                try:
                    self._authority._restore_exchange_database_claim(
                        str(record["displaced_leaf"])
                    )
                except OSError as exc:
                    self._fail_closed(
                        f"rollback re-exchange did not occur: {exc}", record
                    )
                self._revalidate_rollback_target(record)
            except BackupUnclassifiedState as exc:
                self._fail_closed(f"restore rollback failed closed: {exc}", record)
        self._purge_restore_staging(txid)
        record = self._advance(record, "ROLLED_BACK")
        self._write_journal(record)
        return {
            "journal_phase": "RESTORE_ROLLBACK_INTENT",
            "action": "awaiting-restart",
            "restart_required": True,
            "receipt": None,
            "transaction_id": txid,
        }

    def _reconcile_rolled_back(
        self, record: dict[str, object], txid: str, lock_claim, lock_leaf: str
    ) -> dict[str, object]:
        # W9: verify the pre-restore identity, then require a clean restart.
        if not self._live_matches_preservation(record):
            self._fail_closed(
                "rolled-back journal contradicts the live database identity", record
            )
        # The rollback restored the pre-restore installation; the payloads and
        # lifecycle artefacts this transaction published before adoption are
        # now unattributable residue and are withdrawn by recorded plan.
        self._withdraw_staged_attachment_state(record)
        self._purge_restore_staging(txid)
        self._sweep_transaction_temp_leaves(txid)
        self._unlink_journal()
        self._release_transaction_lock(lock_claim, lock_leaf, unlink=True)
        return {
            "journal_phase": "ROLLED_BACK",
            "action": "awaiting-restart",
            "restart_required": True,
            "receipt": None,
            "transaction_id": txid,
        }

    # ------------------------------------------------------------------
    # Destructive-transaction reconciliation (D-D=D.2): a journal with a null
    # preservation record past the scratch phases.  There is no rollback
    # source, so attribution is by the recorded candidate content digest and
    # the rollback machine is never entered: a post-adoption failure fails
    # closed (D-E=E.1 has no rollback source to guess from).
    # ------------------------------------------------------------------

    def _reconcile_destructive_adopt_intent(
        self, record: dict[str, object], txid: str, lock_claim, lock_leaf: str
    ) -> dict[str, object]:
        # W4/W5 for a destructive transaction.  The adoption exchange is
        # atomic: the canonical leaf is either the candidate (exchange
        # happened — converge forward) or the pre-exchange installation
        # (exchange did not happen — no mutation occurred, abort cleanly).
        candidate_digest = self._package_database_digest(record)
        if self._live_matches_digest(candidate_digest):
            record = self._advance(record, "PROMOTED")
            self._write_journal(record)
            return self._reconcile_destructive_forward(
                record, txid, lock_claim, lock_leaf
            )
        if candidate_digest is None:
            self._fail_closed(
                "interrupted destructive adoption cannot be attributed: the "
                "recorded package no longer yields a candidate digest",
                record,
            )
        # Not the candidate ⇒ by exchange atomicity the canonical leaf is the
        # pre-exchange installation: abort without live mutation.  The
        # operator may re-run the override; nothing was destroyed.  The
        # staged payloads and lifecycle artefacts are withdrawn by recorded
        # plan (B2/B3).
        self._withdraw_staged_attachment_state(record)
        self._discard_candidate(record)
        self._purge_restore_staging(txid)
        self._sweep_transaction_temp_leaves(txid)
        self._unlink_journal()
        self._release_transaction_lock(lock_claim, lock_leaf, unlink=True)
        return {
            "journal_phase": "ADOPT_INTENT",
            "action": "aborted",
            "restart_required": False,
            "receipt": None,
            "transaction_id": txid,
        }

    def _reconcile_destructive_promoted(
        self, record: dict[str, object], txid: str, lock_claim, lock_leaf: str
    ) -> dict[str, object]:
        candidate_digest = self._package_database_digest(record)
        if self._live_matches_digest(candidate_digest):
            return self._reconcile_destructive_forward(
                record, txid, lock_claim, lock_leaf
            )
        self._fail_closed(
            "destructive promotion cannot be attributed by recorded identity",
            record,
        )

    def _reconcile_destructive_forward(
        self, record: dict[str, object], txid: str, lock_claim, lock_leaf: str
    ) -> dict[str, object]:
        """W6/W8 for a destructive transaction: validate, destroy, commit."""
        entry_phase = str(record["phase"])
        if entry_phase == "PROMOTED":
            try:
                self._validate_promoted(record)
            except Exception as validation_error:
                # D-E=E.1 with no rollback source: fail closed, never guess,
                # never fabricate a rollback that was durably waived.
                self._fail_closed(
                    "destructive restore post-adoption validation failed and "
                    f"no rollback source exists: {validation_error}",
                    record,
                )
            record = self._advance(record, "POST_ADOPTION_VALIDATED")
            self._write_journal(record)
        if str(record["phase"]) == "POST_ADOPTION_VALIDATED":
            record = self._advance(record, "CLEANUP_PENDING")
            self._write_journal(record)
        self._purge_restore_staging(txid)
        # The authorized destructive consequence: the displaced pre-restore
        # installation (sitting at the exchanged candidate leaf) is destroyed,
        # so the current installation truthfully does not remain
        # rollback-capable after the override completes.
        self._destroy_displaced_installation(record)
        receipt = self._build_receipt(record, outcome="RESTORED_DESTRUCTIVE")
        self._write_receipt(receipt, txid)
        record = self._advance(record, "RESTORE_COMMITTED")
        self._write_journal(record)
        self._unlink_journal()
        self._release_transaction_lock(lock_claim, lock_leaf, unlink=True)
        return {
            "journal_phase": entry_phase,
            "action": "completed",
            "restart_required": False,
            "receipt": receipt,
            "transaction_id": txid,
        }

    def _destroy_displaced_installation(self, record: dict[str, object]) -> None:
        """D-D=D.2 consequence: destroy the displaced pre-restore database.

        After the adoption exchange the displaced installation lives at the
        recorded candidate leaf.  The destructive override waives its
        preservation (I15), so the bytes are unlinked and the database
        directory fsynced.  Absence (a crash between unlink and commit) is
        the already-destroyed state and is tolerated.
        """
        database_fd = self._database_fd()
        leaf = str(record["displaced_leaf"])
        try:
            claim, fd, _ = self._open_regular(database_fd, leaf)
        except FileNotFoundError:
            return
        self._release_fd(claim)
        os.unlink(leaf, dir_fd=database_fd)
        os.fsync(database_fd)

    # ------------------------------------------------------------------
    # Coordinator-facing startup surface (M3): receipt finalisation and the
    # D-D=D.2 destructive-override gate.  Finalisation is a receipt-record
    # update only — no authoritative data mutation, no lifecycle state, no
    # journal reopen (RESTORE_STATE_MACHINE.md section 3.1).  The override
    # gate is evaluation only: it never mutates state and never substitutes
    # for the normal preservation path (DESTRUCTIVE_INVARIANTS.md I15).
    # ------------------------------------------------------------------

    def _observe_canonical_revision(self) -> str:
        """Read-only observation of the canonical database revision.

        Opens the rooted VFS under the authority exactly like the
        post-adoption validation battery, reads ``alembic_version``, and
        closes everything before returning.  No authoritative mutation.
        """
        from bots5.infrastructure.persistence.migration_runner import _MIGRATION_CHAIN

        authority = self._authority
        authority._claim_database()
        vfs = authority._open_rooted_vfs()
        rows: list[tuple[object, ...]] = []
        try:
            connection = vfs.connect()
            try:
                rows = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchall()
            finally:
                connection.close()
            if vfs.open_count != 0:
                raise BackupUnclassifiedState(
                    "canonical revision observation left database files open"
                )
        finally:
            authority._close_database_vfs()
        authority.database_durability_fence()
        if (
            len(rows) != 1
            or type(rows[0][0]) is not str
            or rows[0][0] not in _MIGRATION_CHAIN
        ):
            raise BackupUnclassifiedState(
                "canonical database revision is unattributable for receipt finalisation"
            )
        return str(rows[0][0])

    def finalise_restore_receipt(self) -> dict[str, object] | None:
        """Finalise the restore receipt once the adoption has reached head.

        At ``RESTORE_COMMITTED`` the receipt records
        ``post_adoption_migration = "required"`` when the captured revision
        was below the application head.  Under the adjudicated D-B=B.1
        ordering the forward migration runs inside a later startup's
        ``open_store()``, so this method finalises the receipt on a later
        startup by reconciling the migration machine's durable evidence:

        - ``{"action": "finalised", ...}`` once the adopted installation is
          observed at the application head with a converged migration
          machine: ``post_adoption_migration = "completed"`` and
          ``target_revision_after_migration`` are recorded.
        - ``{"action": "failed_rolled_back", ...}`` when the durable
          migration recovery journal (``phase6-journal-v3.json``) attributes
          a failed, rolled-back post-adoption migration to this receipt:
          ``post_adoption_migration = "failed_rolled_back"`` is recorded
          together with the migration recovery reference (the migration
          journal's transaction id).
        - ``{"action": "pending_migration", ...}`` while the adopted
          installation is still below head and no attributable failure
          evidence exists.
        - ``{"action": "migration_recovery_pending", ...}`` while the
          migration machine holds an unconverged journal, so the observed
          revision is not yet a machine-validated completion.

        Returns ``None`` when no receipt exists or the receipt is already
        terminal.  A ``ROLLED_BACK`` migration journal that cannot be
        attributed to this receipt raises fail-closed and leaves the
        receipt unchanged: a terminal failure state is never fabricated.
        Finalisation is always a receipt-record update only — no
        authoritative data mutation, no lifecycle state, no journal reopen.
        """
        with self._authority.transition():
            return self._finalise_restore_receipt()

    def _read_migration_journal_record(self) -> dict[str, object] | None:
        """Read the migration recovery journal through its own machine.

        The migration runner owns ``database/migration/phase6-journal-v3.json``
        and is the only interpretation of it; this uses the runner's validated
        reader (closed phase schema, canonical bytes, live authority identity
        attribution) so no parallel interpretation is invented here.  A journal
        that cannot be read or attributed is ambiguous evidence and fails
        closed for receipt finalisation.
        """
        from bots5.infrastructure.persistence.migration_runner import _read_journal

        try:
            return _read_journal(self._authority)
        except RuntimeError as exc:
            raise BackupUnclassifiedState(
                "migration recovery journal is unattributable for receipt "
                f"finalisation: {exc}"
            ) from exc

    def _canonical_matches_migration_source(
        self, record: dict[str, object]
    ) -> bool:
        """Prove the canonical database is the journal's pre-migration source."""
        from bots5.infrastructure.persistence.migration_runner import _source_matches

        authority = self._authority
        authority._claim_database()
        try:
            return _source_matches(
                authority,
                authority._database_dir_capability,
                "state.sqlite3",
                record,
            )
        except RuntimeError as exc:
            raise BackupUnclassifiedState(
                "canonical database cannot be attributed against the migration "
                f"recovery journal: {exc}"
            ) from exc

    def _attribute_rolled_back_post_adoption_migration(
        self,
        journal: dict[str, object],
        receipt: dict[str, object],
        revision: str,
    ) -> str:
        """Attribute a journaled migration rollback to this receipt, or fail.

        Under D-B=B.1 the forward migration of the adopted installation runs
        inside a later startup's ``open_store()``.  When it fails, the existing
        migration machine restores the pre-migration source and durably
        journals ``ROLLED_BACK`` (``restored=True``) before its terminal
        cleanup removes the journal.  The failure is attributable to THIS
        receipt's post-adoption migration only when every element of the
        evidence chain holds:

        - the journal is a validated ``EXISTING``-source migration targeting
          the application head (the post-adoption migration);
        - it started from exactly ``target_revision_at_commit`` — the revision
          this receipt adopted;
        - the canonical database is still observed at that adopted revision;
        - the canonical database bytes and identity match the journal's
          recorded pre-migration source.

        Only then is the journal's transaction id returned as the migration
        recovery reference.  A ``ROLLED_BACK`` journal that fails any element
        cannot be attributed: raise fail-closed and leave the receipt
        unchanged — a terminal failure state is never fabricated.
        """
        from bots5.infrastructure.persistence.migration_runner import _HEAD

        committed = str(receipt["target_revision_at_commit"])
        if (
            str(journal["source_kind"]) == "EXISTING"
            and str(journal["target_revision"]) == _HEAD
            and str(journal["expected_start_revision"]) == committed
            and revision == committed
            and self._canonical_matches_migration_source(journal)
        ):
            return str(journal["transaction_id"])
        raise BackupUnclassifiedState(
            "a rolled-back post-adoption migration cannot be attributed to "
            "this restore receipt by the recorded recovery evidence"
        )

    def _rewrite_finalised_receipt(self, finalised: dict[str, object]) -> None:
        txid = str(finalised["transaction_id"])
        # A crash between the temp write and the rename leaves exactly this
        # deterministic temp leaf behind; with the receipt present it can only
        # be debris of an interrupted finalisation of this same receipt.
        temp_leaf = f".bots5-restore-receipt-{txid}.tmp"
        try:
            os.unlink(temp_leaf, dir_fd=self._database_fd())
        except FileNotFoundError:
            pass
        else:
            os.fsync(self._database_fd())
        self._write_receipt(finalised, txid)

    def _finalise_restore_receipt(self) -> dict[str, object] | None:
        from bots5.infrastructure.persistence.migration_runner import _HEAD

        receipt = self._read_receipt()
        if receipt is None:
            return None
        state = receipt["post_adoption_migration"]
        if state != "required":
            # not_required, completed and failed_rolled_back are terminal
            # receipt states; finalisation never rewrites them.
            return None
        if receipt["outcome"] not in {"RESTORED", "RESTORED_DESTRUCTIVE"}:
            raise BackupUnclassifiedState(
                "restore receipt outcome is unattributable for finalisation"
            )
        revision = self._observe_canonical_revision()
        journal = self._read_migration_journal_record()
        if revision != _HEAD:
            if journal is not None and str(journal["phase"]) == "ROLLED_BACK":
                reference = self._attribute_rolled_back_post_adoption_migration(
                    journal, receipt, revision
                )
                if reference is not None:
                    # RESTORE_STATE_MACHINE.md section 3.1: the forward
                    # migration failed and rolled back; record the terminal
                    # failure state together with the migration recovery
                    # reference.  Receipt-record update only — no
                    # authoritative data mutation, no journal reopen.
                    finalised = dict(receipt)
                    finalised["post_adoption_migration"] = "failed_rolled_back"
                    finalised["migration_recovery_reference"] = reference
                    self._rewrite_finalised_receipt(finalised)
                    return {
                        "action": "failed_rolled_back",
                        "receipt": finalised,
                        "migration_recovery_reference": reference,
                    }
            # B.1: the forward migration of the adopted installation has not
            # durably completed and no attributable failure evidence exists;
            # this startup's open_store() runs (or resumes) it and the
            # finalisation happens on a later startup.
            return {
                "action": "pending_migration",
                "transaction_id": receipt["transaction_id"],
                "revision": revision,
            }
        if journal is not None:
            # The migration machine has not converged (an interrupted
            # PREPARING..RESTORE_INTENT or COMMITTED journal is present): the
            # observed head revision is not yet a machine-validated
            # completion, so recording "completed" now could fabricate a
            # terminal success that an immediate rollback invalidates.
            # Defer; open_store() converges the machine and a later startup
            # finalises.  The receipt stays unchanged.
            return {
                "action": "migration_recovery_pending",
                "transaction_id": receipt["transaction_id"],
                "journal_phase": str(journal["phase"]),
                "revision": revision,
            }
        finalised = dict(receipt)
        finalised["post_adoption_migration"] = "completed"
        finalised["target_revision_after_migration"] = revision
        self._rewrite_finalised_receipt(finalised)
        return {"action": "finalised", "receipt": finalised}

    def evaluate_destructive_override(
        self, *, operator_authorized: bool
    ) -> dict[str, object]:
        """Evaluate the D-D=D.2 destructive-override gate (invariant I15).

        Preservation is the normal required path and there is no automatic
        fallback; the override exists only as a separate, explicit operator
        authorization and may proceed only when ALL FOUR conditions hold:
        the target authoritative installation/root identity is proven, the
        Backup v1 source has independently verified, B.O.T.S. can truthfully
        report that the current installation will not remain
        rollback-capable, and the operator explicitly authorized that
        destructive consequence.  Authority/target-identity uncertainty
        fails closed (poison) even when the override is supplied.

        Evaluation is read-only and refuses without mutating anything; it is
        consumed by the restore transaction owner, which re-proves the grant
        under its own transaction lock.  The ordinary restore path never
        consults this gate.
        """
        if type(operator_authorized) is not bool:
            raise TypeError(
                "operator authorization must be an explicitly supplied bool"
            )
        with self._authority.transition():
            return self._evaluate_destructive_override(operator_authorized)

    def _destructive_override_conditions(
        self, operator_authorized: bool
    ) -> dict[str, object]:
        """Re-prove the four D-D=D.2 validity conditions (invariant I15).

        Shared verbatim by the read-only evaluation gate and the M5
        destructive execution, so the execution path can never prove weaker
        conditions than the gate.  Order matters and is part of the contract:
        identity uncertainty poisons the authority even when the operator
        authorized the override; the authorization itself is checked before
        the (possibly expensive) source re-verification.
        """
        from bots5.infrastructure.backup_package import BackupZipPackageAdapter
        from bots5.infrastructure.persistence.migration_runner import _MIGRATION_CHAIN

        # Condition 1 (fail closed, override notwithstanding): the target
        # authoritative installation/root identity is proven.  A torn or
        # non-canonical journal, a poisoned authority, or any mismatch
        # between the recorded and live identities is uncertainty, which the
        # override may never waive (I5, I15).
        try:
            existing = self._read_journal()
        except BackupUnclassifiedState:
            self._authority.poison(
                "destructive override refused: restore journal is unattributable"
            )
            raise
        if existing is None:
            raise BackupError(
                "destructive override refused: no pending restore transaction to evaluate"
            )
        record, _raw = existing
        try:
            self._authority.assert_live()
            self._authority._claim_database()
            observed = self._directory_identity_records()
        except BackupUnclassifiedState:
            self._authority.poison(
                "destructive override refused: target identity is uncertain"
            )
            raise
        for name, value in observed.items():
            if value != record["directory_identities"][name]:
                self._authority.poison(
                    "destructive override refused: recorded root identity no longer "
                    "matches the authority"
                )
                raise BackupUnclassifiedState(
                    "destructive override refused: recorded root identity no longer "
                    "matches the authority"
                )
        # Condition 4: the separate explicit operator authorization for the
        # destructive consequence, supplied independently of any restore
        # request.  It is never defaulted and never implied.
        if not operator_authorized:
            raise BackupError(
                "destructive override refused: the destructive consequence is not "
                "explicitly authorized by the operator"
            )
        # Condition 2: the Backup v1 source has independently verified
        # (artifact-only verification, I1) and still matches the recorded
        # transaction.
        package_path = Path(str(record["package_path"]))
        try:
            verified = BackupZipPackageAdapter().verify(
                package_path, expected_backup_id=str(record["backup_id"])
            )
        except (OSError, BackupError) as exc:
            raise BackupError(
                "destructive override refused: Backup v1 source did not "
                "independently verify"
            ) from exc
        if (
            verified.artifact_sha256 != record["package_sha256"]
            or str(verified.backup_id) != str(record["backup_id"])
            or str(verified.source_db_migration_revision)
            != str(record["target_db_revision"])
            or str(record["target_db_revision"]) not in _MIGRATION_CHAIN
        ):
            raise BackupError(
                "destructive override refused: verified source does not match the "
                "recorded restore transaction"
            )
        # Condition 3: B.O.T.S. truthfully reports that the current
        # installation will not remain rollback-capable.  Only a transaction
        # that never durably established a preservation record qualifies; a
        # recorded preservation means the normal rollback-capable path
        # applies, and a failed-closed transaction requires human inspection.
        phase = str(record["phase"])
        if phase == "RESTORE_FAILED_CLOSED":
            raise BackupError(
                "destructive override refused: the pending transaction is "
                "failed-closed and requires human inspection"
            )
        if phase != "PRESERVING" or record["preservation_record"] is not None:
            raise BackupError(
                "destructive override refused: the current installation remains "
                "rollback-capable; the normal restore path applies"
            )
        return {
            "record": record,
            "package_path": package_path,
            "conditions": {
                "target_identity_proven": True,
                "source_verified": True,
                "not_rollback_capable": True,
                "operator_authorized": True,
            },
        }

    def _evaluate_destructive_override(
        self, operator_authorized: bool
    ) -> dict[str, object]:
        facts = self._destructive_override_conditions(operator_authorized)
        record = facts["record"]
        assert isinstance(record, dict)
        return {
            "granted": True,
            "transaction_id": str(record["restore_transaction_id"]),
            "backup_id": str(record["backup_id"]),
            "package_sha256": str(record["package_sha256"]),
            "journal_phase": str(record["phase"]),
            "conditions": facts["conditions"],
        }

    # ------------------------------------------------------------------
    # D-D=D.2 destructive-override execution (M5, invariant I15).  No
    # automatic fallback exists: the normal restore path never consults this
    # surface.  Execution re-proves all four validity conditions and then —
    # under the whole-transaction flock — discards the unrecorded W2 scratch,
    # proves the absence of any rollback source, and runs the transaction
    # through staging, the fsynced ADOPT_INTENT boundary, the one
    # RENAME_EXCHANGE and post-adoption validation WITHOUT preservation.  A
    # post-adoption failure has no rollback source to restore and therefore
    # fails closed (D-E=E.1); the displaced installation is destroyed before
    # the receipt is written so the "not rollback-capable" statement is
    # truthful after the override completes.
    # ------------------------------------------------------------------

    def execute_destructive_override(
        self, *, operator_authorized: bool
    ) -> dict[str, object]:
        """Execute the D-D=D.2 destructive continuation of a halted restore.

        The only legitimate entry state is a journaled transaction still at
        ``PRESERVING`` with no preservation record: preservation did not
        complete, so the current installation is known and intact but this
        transaction has no rollback source for it.  With every validity
        condition re-proved — target identity proven (uncertainty still fails
        closed even under the override), source independently verified,
        rollback capability truthfully absent after the unrecorded scratch is
        discarded, and the separate explicit operator authorization — the
        transaction is continued destructively to ``RESTORE_COMMITTED`` with
        the displaced installation destroyed.  Any refusal leaves the
        transaction and the live installation exactly as recorded.
        """
        if type(operator_authorized) is not bool:
            raise TypeError(
                "operator authorization must be an explicitly supplied bool"
            )
        with self._authority.transition():
            return self._execute_destructive_override(operator_authorized)

    def _discard_unrecorded_preservation_scratch(
        self, record: dict[str, object]
    ) -> None:
        """Discard a partial W2 preservation artefact (never identity-recorded)."""
        retained_fd = self._authority._retained_installations_dir_capability
        leaf = self._preservation_leaf(record)
        try:
            claim, fd, _ = self._open_regular(retained_fd, leaf)
        except FileNotFoundError:
            return
        self._release_fd(claim)
        os.unlink(leaf, dir_fd=retained_fd)
        os.fsync(retained_fd)

    def _execute_destructive_override(
        self, operator_authorized: bool
    ) -> dict[str, object]:
        facts = self._destructive_override_conditions(operator_authorized)
        record: dict[str, object] = facts["record"]  # type: ignore[assignment]
        assert isinstance(record, dict)
        txid = str(record["restore_transaction_id"])
        lock_leaf = self._lock_leaf(txid)
        lock_claim = self._open_transaction_lock(txid)
        try:
            return self._execute_destructive_transaction(
                record, facts["package_path"], lock_claim, lock_leaf
            )
        finally:
            try:
                self._release_transaction_lock(lock_claim, lock_leaf, unlink=False)
            except BaseException:
                self._authority.poison(
                    "restore transaction lock release outcome is uncertain"
                )
                raise

    def _execute_destructive_transaction(
        self,
        record: dict[str, object],
        package_path: Path,
        lock_claim,
        lock_leaf: str,
    ) -> dict[str, object]:
        # Condition 3 hardening under the lock: discard the unrecorded W2
        # scratch, then prove there is no preservation artefact, no staged
        # candidate and no payload staging — i.e. B.O.T.S. can truthfully
        # state that proceeding destroys the current installation without
        # leaving it rollback-capable through this transaction.
        self._discard_unrecorded_preservation_scratch(record)
        retained_fd = self._authority._retained_installations_dir_capability
        preservation_leaf = self._preservation_leaf(record)
        try:
            os.stat(preservation_leaf, dir_fd=retained_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            self._authority.poison(
                "destructive override refused: an unattributable preservation "
                "artefact exists for this transaction"
            )
            raise BackupUnclassifiedState(
                "destructive override refused: an unattributable preservation "
                "artefact exists for this transaction"
            )
        database_fd = self._database_fd()
        staging_fd = self._authority._directory_fd("attachments/staging")
        for uncertain_leaf, uncertain_fd in (
            (str(record["candidate_leaf"]), database_fd),
            (f"restore-{record['restore_transaction_id']}", staging_fd),
        ):
            try:
                os.stat(uncertain_leaf, dir_fd=uncertain_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            self._authority.poison(
                "destructive override refused: the transaction already holds "
                "unattributable staging state"
            )
            raise BackupUnclassifiedState(
                "destructive override refused: the transaction already holds "
                "unattributable staging state"
            )
        # STAGING: the journal's null preservation record from here on is the
        # durable destructive marker.
        record = self._advance(record, "STAGING")
        self._write_journal(record)
        candidate_identity, payloads = self._stage_package(
            record, package_path, str(record["package_sha256"])
        )
        self._validate_candidate(record)
        self._reverify_payload_bijection(record, payloads)
        record = self._advance(record, "STAGED_VALIDATED")
        self._write_journal(record)

        # ADOPT_INTENT: the uncancellable, fsynced boundary.  There is no
        # rollback source to prove (D-E=E.1 applies on failure below).
        self._quiesce()
        record = self._advance(record, "ADOPT_INTENT")
        self._write_journal(record)

        # PROMOTED: exactly one exchange; a raised OSError means the exchange
        # did not occur and is never treated as success (I6/DEFECT-1).
        _fault("before-adopt-exchange")
        try:
            promoted_identity = self._authority._restore_exchange_database_claim(
                str(record["candidate_leaf"])
            )
        except OSError as exc:
            self._fail_closed(
                f"destructive restore exchange did not occur or its outcome is "
                f"unprovable: {exc}",
                record,
            )
        _fault("after-adopt-exchange")
        if (
            promoted_identity.key != candidate_identity.key
            or promoted_identity.size != candidate_identity.size
        ):
            self._fail_closed(
                "promoted database identity does not match the staged candidate",
                record,
            )
        record = self._advance(record, "PROMOTED")
        self._write_journal(record)

        # Post-adoption validation with no rollback machine: failure is
        # RESTORE_FAILED_CLOSED, never a guessed rollback.
        try:
            self._validate_promoted(record)
        except Exception as validation_error:
            self._fail_closed(
                "destructive restore post-adoption validation failed and no "
                f"rollback source exists: {validation_error}",
                record,
            )
        record = self._advance(record, "POST_ADOPTION_VALIDATED")
        self._write_journal(record)

        # CLEANUP_PENDING: private staging is removed and the displaced
        # pre-restore installation is destroyed (the authorized consequence).
        record = self._advance(record, "CLEANUP_PENDING")
        self._write_journal(record)
        self._purge_restore_staging(str(record["restore_transaction_id"]))
        self._destroy_displaced_installation(record)

        # RESTORE_COMMITTED: receipt, terminal journal, journal unlink.
        receipt = self._build_receipt(record, outcome="RESTORED_DESTRUCTIVE")
        self._write_receipt(receipt, str(record["restore_transaction_id"]))
        record = self._advance(record, "RESTORE_COMMITTED")
        self._write_journal(record)
        self._unlink_journal()
        self._release_transaction_lock(lock_claim, lock_leaf, unlink=True)
        return {
            "journal_phase": "PRESERVING",
            "action": "completed",
            "restart_required": False,
            "receipt": receipt,
            "transaction_id": str(record["restore_transaction_id"]),
            "destructive_override": True,
        }

    # ------------------------------------------------------------------
    # D-A=A.2 retention operator consequence (M5).  The displaced
    # installation is retained indefinitely by default: nothing in the
    # restore transaction, reconciliation, receipt finalisation or startup
    # ever calls this.  Removal is a separate, deliberate operator action
    # with an explicit confirmation, never automatic, never time-based and
    # never triggered by storage pressure.
    # ------------------------------------------------------------------

    def remove_retained_installation(
        self, artifact_leaf: str, *, operator_confirmed: bool
    ) -> dict[str, object]:
        """Remove one retained pre-restore installation on operator command.

        ``artifact_leaf`` is the exact preservation leaf name
        ``<txid>-<backup_id>.sqlite3``.  The call refuses — without mutating
        anything — unless the operator explicitly confirmed the destructive
        consequence, the leaf matches the preservation naming contract, the
        artefact is an attributable regular file, and no restore journal
        exists (a mid-transaction journal may still need its rollback
        source, and an unattributable journal means the machine state is not
        known well enough to remove retention state).
        """
        if type(operator_confirmed) is not bool:
            raise TypeError(
                "operator confirmation must be an explicitly supplied bool"
            )
        if not operator_confirmed:
            raise BackupError(
                "retained-installation removal refused: the removal of retained "
                "recovery state requires the explicit operator confirmation"
            )
        match = re.fullmatch(
            rf"({_UUID})-({_UUID})\.sqlite3\Z", artifact_leaf
        )
        if match is None:
            raise BackupError(
                "retained-installation removal refused: the artefact leaf does "
                "not match the preservation naming contract"
            )
        with self._authority.transition():
            return self._remove_retained_installation(artifact_leaf)

    def _remove_retained_installation(self, artifact_leaf: str) -> dict[str, object]:
        # Any restore journal — whatever its phase — keeps the retention
        # surface closed: a non-terminal transaction may still need this
        # artefact as its rollback source, and an unattributable journal
        # means retention state must not be interpreted at all (I5, I16).
        try:
            journal = self._read_journal()
        except BackupUnclassifiedState:
            self._authority.poison(
                "retained-installation removal refused: restore journal is "
                "unattributable"
            )
            raise
        if journal is not None:
            raise BackupError(
                "retained-installation removal refused: a restore journal is "
                "present; the restore machine is not idle"
            )
        retained_fd = self._authority._retained_installations_dir_capability
        for suffix in _SIDECARS:
            try:
                os.stat(
                    f"{artifact_leaf}{suffix}",
                    dir_fd=retained_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            raise BackupUnclassifiedState(
                "retained installation carries SQLite sidecar residue"
            )
        claim, fd, identity = self._open_regular(retained_fd, artifact_leaf)
        try:
            if (
                identity.uid != os.geteuid()
                or stat.S_IMODE(identity.mode) != 0o600
                or identity.nlink != 1
            ):
                raise BackupUnclassifiedState(
                    "retained installation has an unsafe identity"
                )
            size = int(identity.size)
        finally:
            self._release_fd(claim)
        os.unlink(artifact_leaf, dir_fd=retained_fd)
        os.fsync(retained_fd)
        return {
            "removed": f"retained-installations/{artifact_leaf}",
            "bytes": size,
        }
