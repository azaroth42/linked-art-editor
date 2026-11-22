"""Smoke test: the test database is reachable and has the deploy/postgres/init.sql extensions."""

import asyncpg
import pytest

REQUIRED_EXTENSIONS = {"vector", "pg_trgm", "btree_gin"}


@pytest.mark.integration
async def test_test_database_has_required_extensions(db_url: str) -> None:
    dsn = db_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("SELECT extname FROM pg_extension")
    finally:
        await conn.close()
    assert REQUIRED_EXTENSIONS <= {row["extname"] for row in rows}
