from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


_HEAD = "0005_generation_outcomes"
_SUPPORTED_REVISIONS = {
    "0001_desktop_state",
    "0002_conversation_lineage",
    "0003_integrity_boundaries",
    "0004_integrity_guard_function",
    _HEAD,
}


def _read_revision(database: Path) -> str | None:
    if not database.exists() or database.stat().st_size == 0:
        return None
    try:
        with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"cannot inspect database schema: {database}") from exc
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeError(f"database has an invalid Alembic revision set: {database}")
    return str(rows[0][0])


def _logical_digest(database: Path) -> str:
    digest = hashlib.sha256()
    try:
        with sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro", uri=True
        ) as connection:
            for line in connection.iterdump():
                digest.update(line.encode("utf-8"))
                digest.update(b"\n")
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"cannot fingerprint SQLite database: {database}") from exc
    return digest.hexdigest()


def _recovery_metadata_path(recovery_point: Path) -> Path:
    return recovery_point.with_name(f"{recovery_point.name}.json")


def _verify_recovery_point(
    recovery_point: Path,
    metadata_path: Path,
    expected_digest: str,
) -> None:
    try:
        with sqlite3.connect(
            f"{recovery_point.resolve().as_uri()}?mode=ro", uri=True
        ) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise RuntimeError(
                    f"recovery point failed SQLite integrity check: {recovery_point}"
                )
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"cannot verify recovery point: {recovery_point}") from exc

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read recovery-point metadata: {metadata_path}") from exc
    if not isinstance(metadata, dict) or metadata.get("format") != 1:
        raise RuntimeError(f"unsupported recovery-point metadata: {metadata_path}")
    if metadata.get("source_digest") != expected_digest:
        raise RuntimeError(
            "recovery point does not match the database being migrated: "
            f"{recovery_point}"
        )
    recovery_digest = _logical_digest(recovery_point)
    if metadata.get("recovery_digest") != recovery_digest or recovery_digest != expected_digest:
        raise RuntimeError(f"recovery point content does not match its metadata: {recovery_point}")


def _write_recovery_metadata(
    path: Path,
    *,
    source_digest: str,
    recovery_digest: str,
    schema_revision: str | None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "format": 1,
                "source_digest": source_digest,
                "recovery_digest": recovery_digest,
                "schema_revision": schema_revision,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _create_recovery_point(database: Path) -> None:
    recovery_point = database.with_name(f".{database.name}.pre-migration")
    metadata_path = _recovery_metadata_path(recovery_point)
    temporary = recovery_point.with_name(f".{recovery_point.name}.tmp")
    temporary_metadata = metadata_path.with_name(f".{metadata_path.name}.tmp")
    source_digest = _logical_digest(database)
    if recovery_point.exists():
        if metadata_path.exists():
            _verify_recovery_point(recovery_point, metadata_path, source_digest)
            return
        recovery_digest = _logical_digest(recovery_point)
        if recovery_digest != source_digest:
            raise RuntimeError(
                f"recovery point cannot be verified without matching metadata: {recovery_point}"
            )
        if temporary_metadata.exists():
            _verify_recovery_point(recovery_point, temporary_metadata, source_digest)
        else:
            _write_recovery_metadata(
                temporary_metadata,
                source_digest=source_digest,
                recovery_digest=recovery_digest,
                schema_revision=_read_revision(database),
            )
            _verify_recovery_point(recovery_point, temporary_metadata, source_digest)
        os.replace(temporary_metadata, metadata_path)
        return
    if metadata_path.exists():
        raise RuntimeError(f"orphaned recovery-point metadata exists: {metadata_path}")
    if temporary_metadata.exists() and not temporary.exists():
        temporary_metadata.unlink()
    try:
        if not temporary.exists():
            with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as source:
                with sqlite3.connect(temporary) as destination:
                    source.backup(destination)
            shutil.copymode(database, temporary)
        recovery_digest = _logical_digest(temporary)
        if recovery_digest != source_digest:
            raise RuntimeError("recovery point fingerprint differs from the source database")
        if not temporary_metadata.exists():
            _write_recovery_metadata(
                temporary_metadata,
                source_digest=source_digest,
                recovery_digest=recovery_digest,
                schema_revision=_read_revision(database),
            )
        _verify_recovery_point(temporary, temporary_metadata, source_digest)
        os.replace(temporary, recovery_point)
        os.replace(temporary_metadata, metadata_path)
    except (OSError, sqlite3.DatabaseError) as exc:
        raise RuntimeError(f"cannot create verified recovery point: {recovery_point}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()
        if temporary_metadata.exists() and not recovery_point.exists():
            temporary_metadata.unlink()


def _restore_recovery_point(
    database: Path,
    recovery_point: Path,
    metadata_path: Path,
) -> None:
    expected_digest = _logical_digest(recovery_point)
    _verify_recovery_point(recovery_point, metadata_path, expected_digest)
    temporary = database.with_name(f".{database.name}.restore.tmp")
    if temporary.exists():
        raise RuntimeError(f"temporary database restore already exists: {temporary}")
    try:
        with sqlite3.connect(
            f"{recovery_point.resolve().as_uri()}?mode=ro", uri=True
        ) as source:
            with sqlite3.connect(temporary) as destination:
                source.backup(destination)
        shutil.copymode(database, temporary)
        if _logical_digest(temporary) != expected_digest:
            raise RuntimeError("restored database fingerprint differs from the recovery point")
        os.replace(temporary, database)
    finally:
        if temporary.exists():
            temporary.unlink()


def upgrade_database(database: Path | str) -> None:
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    current_revision = _read_revision(database)
    if current_revision is not None and current_revision not in _SUPPORTED_REVISIONS:
        raise RuntimeError(
            f"database schema revision is newer or unsupported: {current_revision}"
        )
    recovery_point: Path | None = None
    if database.exists() and database.stat().st_size > 0 and current_revision != _HEAD:
        _create_recovery_point(database)
        recovery_point = database.with_name(f".{database.name}.pre-migration")
    metadata_path = None if recovery_point is None else _recovery_metadata_path(recovery_point)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    try:
        command.upgrade(config, _HEAD)
    except Exception:
        if recovery_point is not None and metadata_path is not None:
            try:
                _restore_recovery_point(database, recovery_point, metadata_path)
            except Exception as restore_exc:
                raise RuntimeError(
                    f"migration failed and recovery restore failed: {database}"
                ) from restore_exc
        raise
