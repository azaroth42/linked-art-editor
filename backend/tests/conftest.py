"""Shared pytest fixtures.

Database tests use only the database named by ``LAE_TEST_DATABASE_URL``
(``postgresql+asyncpg://user:password@host:port/dbname``). CI provisions a native PostgreSQL 16
with pgvector; locally, point it at your slot database, e.g.
``postgresql+asyncpg://localhost:5432/lae_i<N>_test`` (docs/agent-workflow.md). There is no
container fallback (ADR-0017).

Once per session ``db_url`` drops and recreates that database, applies
``deploy/postgres/init.sql``, and runs the migrations; the database is dropped when the session
ends, so its name must end in ``_test``. Every test that uses ``db_url`` (directly or through
``db_engine`` or ``db_session``) is marked ``integration``. When the variable is unset those
tests are skipped with the reason, except under CI (``CI`` set), where they fail; every other
test runs.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from lae.db.migrate import upgrade

TEST_DATABASE_URL_ENV = "LAE_TEST_DATABASE_URL"
INIT_SQL = Path(__file__).resolve().parents[2] / "deploy" / "postgres" / "init.sql"
DATABASE_FIXTURE = "db_url"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test that needs the test database as ``integration``."""
    for item in items:
        if DATABASE_FIXTURE in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.integration)


def _asyncpg_dsn(url: URL) -> str:
    """Turn an SQLAlchemy URL into a DSN that ``asyncpg.connect`` accepts."""
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _drop_database(url: URL) -> None:
    assert url.database is not None
    admin = await asyncpg.connect(_asyncpg_dsn(url.set(database="postgres")))
    try:
        await admin.execute(
            f"DROP DATABASE IF EXISTS {_quote_identifier(url.database)} WITH (FORCE)"
        )
    finally:
        await admin.close()


async def _create_database(url: URL) -> None:
    assert url.database is not None
    admin = await asyncpg.connect(_asyncpg_dsn(url.set(database="postgres")))
    try:
        await admin.execute(f"CREATE DATABASE {_quote_identifier(url.database)}")
    finally:
        await admin.close()
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        await connection.execute(INIT_SQL.read_text())
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    """Recreate the test database with init.sql and every migration; yield its URL; drop it."""
    raw_url = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if not raw_url:
        message = (
            f"{TEST_DATABASE_URL_ENV} is not set. Database tests need a native PostgreSQL "
            "with pgvector, e.g. "
            f"{TEST_DATABASE_URL_ENV}=postgresql+asyncpg://localhost:5432/lae_i<N>_test "
            "(docs/agent-workflow.md)."
        )
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    url = make_url(raw_url)
    if not url.database or not url.database.endswith("_test"):
        pytest.fail(
            f"{TEST_DATABASE_URL_ENV} names database {url.database!r}; the test session "
            "drops and recreates it, so its name must end in '_test'."
        )
    asyncio.run(_drop_database(url))
    asyncio.run(_create_database(url))
    upgrade("head", database_url=raw_url)
    yield raw_url
    asyncio.run(_drop_database(url))


@pytest.fixture
async def db_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    """An async engine on the migrated test database (no pooling across event loops)."""
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """An async session inside a transaction that is rolled back after the test.

    ``session.commit()`` inside the test releases a savepoint, not the outer transaction.
    """
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
