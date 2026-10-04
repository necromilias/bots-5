"""Separate provider-managed plans and Archive v3 receiving-side provenance."""
from alembic import op
from bots5.infrastructure.persistence.provider_managed_schema import (
    TABLE, attribution_guards, installed_definitions, original_reference_guards, rebuild_archive_version_tables,
)

revision = "0020_provider_managed_context"
down_revision = "0019_phase11_generation_settings"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    rebuild_archive_version_tables(connection)
    for name, sql in attribution_guards().items():
        connection.exec_driver_sql(f'DROP TRIGGER {name}')
        connection.exec_driver_sql(sql)
    for name, sql in installed_definitions().items():
        if name in original_reference_guards():
            connection.exec_driver_sql(f'DROP TRIGGER {name}')
        connection.exec_driver_sql(sql)


def downgrade():
    connection = op.get_bind()
    for query in (
        f'SELECT 1 FROM {TABLE} LIMIT 1',
        'SELECT 1 FROM archive_import_operations WHERE archive_version=3 LIMIT 1',
        'SELECT 1 FROM archive_lineage_nodes WHERE source_format=3 LIMIT 1',
        "SELECT 1 FROM generation_attempts WHERE json_extract(request_snapshot,'$.snapshot_version')=5 LIMIT 1",
    ):
        if connection.exec_driver_sql(query).first():
            raise RuntimeError('0020 downgrade refused: provider-managed or Archive v3 evidence exists')
    for name in installed_definitions():
        if name != TABLE:
            connection.exec_driver_sql(f'DROP TRIGGER {name}')
    connection.exec_driver_sql(f'DROP TABLE {TABLE}')
    for sql in original_reference_guards().values():
        connection.exec_driver_sql(sql)
    for name, sql in attribution_guards(legacy=True).items():
        connection.exec_driver_sql(f'DROP TRIGGER {name}')
        connection.exec_driver_sql(sql)
    rebuild_archive_version_tables(connection, downgrade=True)
