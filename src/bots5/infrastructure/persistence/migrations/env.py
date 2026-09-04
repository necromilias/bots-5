from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from bots5.infrastructure.persistence.schema import metadata


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # SQLite cannot rebuild a referenced table with foreign-key actions
        # enabled: dropping the old table would cascade its dependants. The
        # migration itself validates its backfill, creates the constraints,
        # and the runtime store re-enables enforcement on every connection.
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.commit()
        try:
            with connection.begin():
                context.configure(connection=connection, target_metadata=target_metadata)
                with context.begin_transaction():
                    context.run_migrations()
        finally:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
