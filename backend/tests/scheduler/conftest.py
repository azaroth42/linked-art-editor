"""Fixtures for the scheduler and draft reaper tests (Phase 1, Step 1.15).

The reaper and the drafts API run against the records API harness of ``tests/api/conftest.py``:
``network`` is a real ``crdt-ws`` (room manager on the test database) reached in-process, and
``served`` is the ``api`` application whose ``crdt-ws`` calls go through it. Drafts are made idle
by moving ``crdt_drafts.last_activity_at`` (and ``records.created_at``) into the past with
``make_idle``; every test uses fresh records, because all tests share one database and the
reaper acts on every idle draft in it, so tests check their own record ids, never totals.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lae.crdt.client import CrdtWsClient
from lae.db.models import CrdtDraft, Record
from tests.api.conftest import (  # noqa: F401  (fixtures used by the tests of this package)
    CRDT_WS_URL,
    INTERNAL_TOKEN,
    Network,
    network,
    served,
)

TTL_DAYS = 5
"""``draft_ttl_days`` of the tests (the default)."""


@pytest.fixture
def sessions(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory on the test database, as the scheduler process has one."""
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def crdt(network: Network) -> AsyncIterator[CrdtWsClient]:  # noqa: F811
    """The scheduler's ``crdt-ws`` client, reaching the in-process ``crdt-ws``."""
    async with httpx.AsyncClient(transport=network) as http:
        yield CrdtWsClient(http, CRDT_WS_URL, INTERNAL_TOKEN)


async def make_idle(
    engine: AsyncEngine,
    record_id: UUID,
    *,
    days: float = TTL_DAYS + 1,
    contributors: Sequence[UUID] | None = None,
) -> None:
    """Move the draft's last activity (and the record's creation) ``days`` into the past."""
    async with AsyncSession(engine) as session, session.begin():
        await session.execute(
            text(
                "UPDATE crdt_drafts SET last_activity_at = now() - make_interval(secs => :s) "
                "WHERE record_id = :id"
            ),
            {"s": days * 86400, "id": record_id},
        )
        await session.execute(
            text(
                "UPDATE records SET created_at = now() - make_interval(secs => :s) WHERE id = :id"
            ),
            {"s": days * 86400, "id": record_id},
        )
        if contributors is not None:
            await session.execute(
                text("UPDATE crdt_drafts SET contributors = :c WHERE record_id = :id"),
                {"c": list(contributors), "id": record_id},
            )


async def draft_row(engine: AsyncEngine, record_id: UUID) -> CrdtDraft | None:
    """The record's ``crdt_drafts`` row, or ``None``."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await session.get(CrdtDraft, record_id)


async def record_row(engine: AsyncEngine, record_id: UUID) -> Record | None:
    """The ``records`` row, or ``None``."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await session.get(Record, record_id)
