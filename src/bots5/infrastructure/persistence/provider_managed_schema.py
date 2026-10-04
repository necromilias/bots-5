"""0020 additive context evidence and independently guarded attachment admission."""
from __future__ import annotations
import json
import re

from bots5.core.provider_managed_context import ProviderManagedContextPlan, canonical_json

REVISION = "0020_provider_managed_context"
TABLE = "provider_managed_context_plans"

TABLE_SQL = f"""CREATE TABLE {TABLE} (
 attempt_id TEXT PRIMARY KEY REFERENCES generation_attempts(id) ON DELETE RESTRICT,
 accounting_mode TEXT NOT NULL CHECK(accounting_mode='provider-managed'),
 plan_version INTEGER NOT NULL CHECK(plan_version=1),
 plan_json TEXT NOT NULL CHECK(json_valid(plan_json)),
 canonical_representation TEXT NOT NULL CHECK(json_valid(canonical_representation)),
 canonical_digest TEXT NOT NULL CHECK(length(canonical_digest)=64),
 created_at TEXT NOT NULL
)"""

INSERT_SQL = f"""CREATE TRIGGER provider_managed_plan_insert_guard BEFORE INSERT ON {TABLE}
 WHEN bots5_provider_managed_plan_valid(NEW.plan_json)<>1
 OR NEW.accounting_mode<>'provider-managed' OR NEW.plan_version<>1
 OR json_extract(NEW.plan_json,'$.canonical_representation') IS NOT NEW.canonical_representation
 OR json_extract(NEW.plan_json,'$.canonical_digest') IS NOT NEW.canonical_digest
 OR EXISTS(SELECT 1 FROM context_plans WHERE attempt_id=NEW.attempt_id)
 OR NOT EXISTS(SELECT 1 FROM generation_attempts a JOIN messages m ON m.id=a.user_message_id
   WHERE a.id=NEW.attempt_id AND json_extract(a.request_snapshot,'$.snapshot_version')=5
   AND json_extract(a.request_snapshot,'$.accounting_mode')='provider-managed'
   AND json_extract(a.request_snapshot,'$.provider_managed_plan')=json(NEW.plan_json)
   AND json_extract(NEW.plan_json,'$.parent_id') IS m.parent_id)
 BEGIN SELECT RAISE(ABORT,'provider-managed plan requires its matching v5 attempt'); END"""

UPDATE_SQL = f"""CREATE TRIGGER provider_managed_plan_update_guard BEFORE UPDATE ON {TABLE}
 BEGIN SELECT RAISE(ABORT,'provider-managed context evidence is immutable'); END"""
DELETE_SQL = f"""CREATE TRIGGER provider_managed_plan_delete_guard BEFORE DELETE ON {TABLE}
 WHEN bots5_phase11_chat_message_delete_allowed((SELECT chat_id FROM generation_attempts WHERE id=OLD.attempt_id))=0
 BEGIN SELECT RAISE(ABORT,'provider-managed context evidence is immutable'); END"""
EXCLUSIVE_SQL = f"""CREATE TRIGGER provider_managed_exact_exclusion_guard BEFORE INSERT ON context_plans
 WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE attempt_id=NEW.attempt_id)
 BEGIN SELECT RAISE(ABORT,'attempt already owns provider-managed evidence'); END"""


def original_reference_guards():
    from .phase6_schema import DDL as exact
    from .phase9_schema import DDL as archive
    message = next(s for s in exact if 'CREATE TRIGGER IF NOT EXISTS phase6_message_attachment_insert_guard' in s)
    attempt = next(s for s in archive if 'CREATE TRIGGER phase6_attempt_attachment_insert_guard' in s)
    return {"phase6_message_attachment_insert_guard": message, "phase6_attempt_attachment_insert_guard": attempt}


def reference_guards():
    result = {}
    for name, original in original_reference_guards().items():
        is_attempt = name == "phase6_attempt_attachment_insert_guard"
        owner = 'a.id=NEW.attempt_id' if is_attempt else 'a.user_message_id=NEW.message_id'
        # Preserve the existing native/imported continuation ownership arm
        # verbatim for attempt references, independently of accounting kind.
        ownership = ''
        if is_attempt:
            start = original.index('AND (EXISTS (SELECT 1 FROM message_attachments')
            ownership = original[start:original.rindex(') BEGIN SELECT')]
        arm = f"""AND NOT EXISTS (
          SELECT 1 FROM generation_attempts a JOIN {TABLE} p ON p.attempt_id=a.id
          JOIN attachments attachment ON attachment.id=NEW.attachment_id
          JOIN attachment_blobs blob ON blob.digest=attachment.blob_digest AND blob.state='ready'
          JOIN json_each(p.canonical_representation,'$.sources') source
          WHERE {owner} AND json_extract(a.request_snapshot,'$.snapshot_version')=5
          AND json_extract(a.request_snapshot,'$.accounting_mode')='provider-managed'
          AND json_extract(source.value,'$.kind')='attachment'
          AND json_extract(source.value,'$.source_id')=NEW.attachment_id
          AND json_extract(source.value,'$.selected')=1
          AND NEW.ordinal=(SELECT count(*) FROM json_each(p.canonical_representation,'$.sources') prior
            WHERE CAST(prior.key AS INTEGER)<CAST(source.key AS INTEGER)
            AND json_extract(prior.value,'$.kind')='attachment' AND json_extract(prior.value,'$.selected')=1)
          {ownership}
        ) """
        before, after = original.rsplit('BEGIN SELECT', 1)
        result[name] = before + arm + 'BEGIN SELECT' + after
    return result


def installed_definitions():
    return {TABLE: TABLE_SQL, 'provider_managed_plan_insert_guard': INSERT_SQL,
            'provider_managed_plan_update_guard': UPDATE_SQL, 'provider_managed_plan_delete_guard': DELETE_SQL,
            'provider_managed_exact_exclusion_guard': EXCLUSIVE_SQL, **reference_guards()}


def attribution_guards(*, legacy=False):
    """Extend only the closed snapshot-version set of the existing guards."""
    from .phase6_schema import install_phase6_attempt_guards
    statements = []
    class Capture:
        def execute(self, statement):
            statements.append(str(statement))
    install_phase6_attempt_guards(Capture())
    result = {}
    for sql in statements:
        match = re.search(r'CREATE TRIGGER (phase5_attempt_attribution_\w+|generation_attempt_phase5_completion_\w+)', sql)
        if match:
            result[match[1]] = sql if legacy else sql.replace('IN (2, 3)', 'IN (2, 3, 5)')
    return result


def validate_schema(connection):
    from .phase6_schema import _normalise_sql
    for name, expected in installed_definitions().items():
        actual = connection.exec_driver_sql('SELECT sql FROM sqlite_master WHERE name=?', (name,)).scalar_one_or_none()
        # SQLite omits IF NOT EXISTS in stored CREATE statements.
        expected = expected.replace('IF NOT EXISTS ', '')
        if actual is None or _normalise_sql(actual) != _normalise_sql(expected):
            raise RuntimeError(f'provider-managed schema definition mismatch: {name}')
    rows = connection.exec_driver_sql(f'SELECT attempt_id,plan_json,canonical_representation,canonical_digest,created_at FROM {TABLE}').fetchall()
    from bots5.domain.clock import parse_utc
    for attempt_id, plan_json, representation, canonical_digest, created_at in rows:
        plan = ProviderManagedContextPlan.from_evidence(json.loads(plan_json))
        if plan.canonical_representation != representation or plan.canonical_digest != canonical_digest:
            raise RuntimeError('provider-managed durable plan mismatch')
        parse_utc(created_at)
        row = connection.exec_driver_sql('SELECT request_snapshot FROM generation_attempts WHERE id=?', (attempt_id,)).first()
        if row is None or json.loads(row[0]).get('provider_managed_plan') != json.loads(plan_json):
            raise RuntimeError('provider-managed owning snapshot mismatch')
        if connection.exec_driver_sql('SELECT 1 FROM context_plans WHERE attempt_id=?', (attempt_id,)).first():
            raise RuntimeError('conflicting exact/provider-managed ownership')
    missing = connection.exec_driver_sql(f"SELECT a.id FROM generation_attempts a LEFT JOIN {TABLE} p ON p.attempt_id=a.id WHERE json_extract(a.request_snapshot,'$.snapshot_version')=5 AND p.attempt_id IS NULL").first()
    if missing:
        raise RuntimeError('provider-managed attempt lacks its plan')
    # Imported attempts remain inert imported objects, with their original
    # safe source IDs. They must retain the tagged plan that their snapshot owns.
    from bots5.infrastructure.archive_v3_context import validate_provenance, validate_safe_plan
    import hashlib
    for row in connection.exec_driver_sql(
        "SELECT a.id,a.source_attempt_id,a.source_attempt,c.source_plan,c.source_plan_digest,c.local_bindings "
        "FROM archive_imported_attempts a LEFT JOIN archive_imported_context_plans c ON c.attempt_id=a.id"
    ).fetchall():
        local_id, source_id, attempt_json, plan_json, plan_digest, bindings = row
        source_attempt = json.loads(attempt_json)
        provenance = source_attempt.get("request_time_provenance", {})
        plan_row = None if plan_json is None else json.loads(plan_json)
        managed = provenance.get("snapshot_version") == 5
        if plan_row is not None and "accounting_mode" in plan_row and plan_row["accounting_mode"] != "provider-managed":
            raise RuntimeError("unknown imported context accounting mode")
        if not managed and plan_row is not None and plan_row.get("accounting_mode") == "provider-managed":
            raise RuntimeError("imported provider-managed plan contradicts snapshot mode")
        if not managed:
            continue
        validate_provenance(provenance, settings_provenance_values={"application","model","chat_model","branch"})
        if type(plan_row) is not dict or set(plan_row) != {"attempt_id","accounting_mode","plan","created_at"} or plan_row["attempt_id"] != source_id or plan_row["accounting_mode"] != "provider-managed" or plan_row["plan"] != provenance["provider_managed_plan"]:
            raise RuntimeError("imported provider-managed plan ownership mismatch")
        validate_safe_plan(plan_row["plan"])
        parse_utc(plan_row["created_at"])
        if hashlib.sha256(plan_json.encode("utf-8")).hexdigest() != plan_digest or json.loads(bindings) != {"source_attempt_id":source_id,"local_attempt_id":local_id}:
            raise RuntimeError("imported provider-managed plan integrity mismatch")



def persist_plan(connection, *, attempt_id, plan, created_at):
    if not isinstance(plan, ProviderManagedContextPlan):
        raise ValueError('provider-managed plan type required')
    plan.validate()
    connection.exec_driver_sql(f'INSERT INTO {TABLE}(attempt_id,accounting_mode,plan_version,plan_json,canonical_representation,canonical_digest,created_at) VALUES (?,?,?,?,?,?,?)',
        (attempt_id, 'provider-managed', 1, canonical_json(plan.as_evidence()), plan.canonical_representation, plan.canonical_digest, created_at))


def evolve_archive_table_sql(sql: str) -> str:
    return sql.replace('archive_version IN (1,2)', 'archive_version IN (1,2,3)').replace('source_format IN (1,2)', 'source_format IN (1,2,3)')


def rebuild_archive_version_tables(connection, *, downgrade=False):
    """Preserve rows/guards while widening only the two archive-version checks."""
    connection.exec_driver_sql('PRAGMA defer_foreign_keys=ON')
    old_legacy = connection.exec_driver_sql('PRAGMA legacy_alter_table').scalar()
    connection.exec_driver_sql('PRAGMA legacy_alter_table=ON')
    try:
        for table in ('archive_import_operations', 'archive_lineage_nodes'):
            sql = connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).scalar_one()
            objects = connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name", (table,)).fetchall()
            revised = sql.replace('IN (1,2,3)', 'IN (1,2)') if downgrade else evolve_archive_table_sql(sql)
            revised = re.sub(r'CREATE TABLE\s+"?' + table + r'"?', 'CREATE TABLE '+table+'_0020_copy', revised, count=1, flags=re.I)
            connection.exec_driver_sql(revised)
            connection.exec_driver_sql(f'INSERT INTO {table}_0020_copy SELECT * FROM {table}')
            connection.exec_driver_sql(f'DROP TABLE {table}')
            connection.exec_driver_sql(f'ALTER TABLE {table}_0020_copy RENAME TO {table}')
            for (statement,) in objects:
                connection.exec_driver_sql(statement)
        if connection.exec_driver_sql('PRAGMA foreign_key_check').first():
            raise RuntimeError('0020 archive version migration has foreign-key violations')
    finally:
        connection.exec_driver_sql(f'PRAGMA legacy_alter_table={int(old_legacy)}')
