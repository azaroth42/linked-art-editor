"""Tests for ``lae.db.session``: the engine opened by the app lifespan and ``get_session``."""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lae.app import create_app
from lae.db.session import get_session
from lae.settings import Settings


async def test_get_session_yields_a_working_session_per_request(db_url: str) -> None:
    app = create_app(Settings(database_url=db_url, qlever_url=None))
    sessions: list[AsyncSession] = []

    @app.get("/probe")
    async def probe(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
        sessions.append(session)
        database = (await session.execute(text("SELECT current_database()"))).scalar_one()
        return {"database": database}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            first = await client.get("/probe")
            second = await client.get("/probe")
    assert first.status_code == second.status_code == 200
    assert first.json() == {"database": db_url.rsplit("/", 1)[1]}
    assert sessions[0] is not sessions[1]
    assert not sessions[0].in_transaction()  # closed when the request ended
