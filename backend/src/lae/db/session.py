"""The async SQLAlchemy engine and the per-request session dependency.

``create_app`` (``lae.app``) opens one engine per process in its lifespan and stores it, with its
session factory, on ``app.state``; endpoints receive a session through ``Depends(get_session)``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine


def create_engine(database_url: str) -> AsyncEngine:
    """Create the process-wide async engine; connections are checked before each checkout."""
    return _create_async_engine(database_url, pool_pre_ping=True)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return the session factory bound to ``engine`` (objects stay usable after commit)."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: an ``AsyncSession`` for this request, closed when it ends."""
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session
