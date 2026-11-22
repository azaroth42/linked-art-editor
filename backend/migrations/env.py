"""Alembic environment for the lae database, using the async (asyncpg) engine.

The database URL is ``LAE_DATABASE_URL`` (via ``lae.settings``), unless the caller passed one in
``Config.attributes`` (see ``lae.db.migrate``). ``target_metadata`` is the model metadata, so
``alembic revision --autogenerate`` can propose a migration; always hand-check the result.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from lae.db.migrate import DATABASE_URL_ATTRIBUTE
from lae.db.models import Base
from lae.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def database_url() -> str:
    """The URL passed by the caller, else ``LAE_DATABASE_URL``."""
    url: str | None = config.attributes.get(DATABASE_URL_ATTRIBUTE)
    return url if url is not None else get_settings().database_url


def run_migrations_offline() -> None:
    """Emit the migration SQL to stdout (``alembic upgrade head --sql``)."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run the migrations on a synchronous connection facade."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open an async engine and run the migrations on one connection."""
    engine = create_async_engine(database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
