"""Shared pytest fixtures.

Database tests use only the database named by ``LAE_TEST_DATABASE_URL``
(``postgresql+asyncpg://user:password@host:port/dbname``). CI provisions a native PostgreSQL 16
with pgvector; locally, point it at your slot database (docs/agent-workflow.md). There is no
container fallback: when the variable is unset, tests that need the database are skipped.
"""

from __future__ import annotations

import os

import pytest

TEST_DATABASE_URL_ENV = "LAE_TEST_DATABASE_URL"


@pytest.fixture(scope="session")
def db_url() -> str:
    """Return the SQLAlchemy URL of the test database, or skip the test if none is configured."""
    url = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"{TEST_DATABASE_URL_ENV} is not set")
    return url
