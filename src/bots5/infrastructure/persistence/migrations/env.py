from __future__ import annotations

from logging.config import fileConfig
from contextlib import nullcontext

from alembic import context
from sqlalchemy import text

from bots5.infrastructure.persistence.schema import metadata
from bots5.infrastructure.persistence.transition_guard import install_transition_guard


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def run_migrations_offline() -> None:
    raise RuntimeError("B.O.T.S. migrations require an authority-supplied connection")


def run_migrations_online() -> None:
    supplied = config.attributes.get("connection")
    if supplied is None:
        raise RuntimeError("B.O.T.S. migrations require an authority-supplied connection")
    with nullcontext(supplied) as connection:
        install_transition_guard(connection.connection.driver_connection, None)
        # SQLite cannot rebuild a referenced table with foreign-key actions
        # enabled: dropping the old table would cascade its dependants. The
        # migration itself validates its backfill, creates the constraints,
        # and the runtime store re-enables enforcement on every connection.
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.commit()
        try:
            with connection.begin():
                options = {}
                callback = config.attributes.get("on_version_apply")
                if callback is not None:
                    options["on_version_apply"] = callback
                context.configure(
                    connection=connection,
                    target_metadata=target_metadata,
                    **options,
                )
                with context.begin_transaction():
                    context.run_migrations()
        finally:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
