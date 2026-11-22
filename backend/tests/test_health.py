"""Tests for ``GET /health/live`` and ``GET /health/ready`` (Phase 1, Step 1.1).

The app runs in-process with its lifespan. QLever is replaced by an ``httpx.MockTransport``
(no network in unit tests); "Postgres down" is an unreachable URL. Tests that need Postgres up
use the ``db_url`` fixture and are therefore marked ``integration``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
import pytest

import lae.app
from lae.app import QLEVER_READY_QUERY, create_app
from lae.settings import Settings

UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://lae@127.0.0.1:1/lae_unreachable"
QLEVER_URL = "http://qlever.test:7010"

QLeverHandler = Callable[[httpx.Request], Awaitable[httpx.Response]]


async def qlever_up(request: httpx.Request) -> httpx.Response:
    """A QLever that answers ``ASK {}`` like QLever 0.6 does."""
    return httpx.Response(200, json={"boolean": True, "head": {}})


async def no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected outgoing request: {request.method} {request.url}")


@asynccontextmanager
async def serve(
    database_url: str, qlever_url: str | None = None, qlever: QLeverHandler = no_network
) -> AsyncIterator[httpx.AsyncClient]:
    """Run ``create_app`` with its lifespan; yield a client that calls it in-process."""
    settings = Settings(database_url=database_url, qlever_url=qlever_url)
    app = create_app(settings, http_transport=httpx.MockTransport(qlever))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield client


async def test_live_is_ok_even_when_postgres_is_down() -> None:
    async with serve(UNREACHABLE_DATABASE_URL) as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_is_503_naming_postgres_when_postgres_is_down() -> None:
    async with serve(UNREACHABLE_DATABASE_URL) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "failing": ["postgres"],
        "components": {"postgres": "error", "qlever": "not_configured"},
    }


async def test_ready_is_200_with_postgres_up_and_qlever_not_configured(db_url: str) -> None:
    async with serve(db_url) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "failing": [],
        "components": {"postgres": "ok", "qlever": "not_configured"},
    }


async def test_ready_is_200_with_postgres_and_qlever_up(db_url: str) -> None:
    seen: list[httpx.Request] = []

    async def qlever(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return await qlever_up(request)

    async with serve(db_url, QLEVER_URL, qlever) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "failing": [],
        "components": {"postgres": "ok", "qlever": "ok"},
    }
    [request] = seen
    assert request.method == "GET"
    assert str(request.url.copy_with(query=None)) == QLEVER_URL
    assert request.url.params["query"] == QLEVER_READY_QUERY
    assert request.headers["accept"] == "application/sparql-results+json"


async def qlever_refuses(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


async def qlever_errors(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="internal error")


async def qlever_not_sparql(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"something": "else"})


@pytest.mark.parametrize("qlever", [qlever_refuses, qlever_errors, qlever_not_sparql])
async def test_ready_is_503_naming_qlever_when_qlever_fails(
    db_url: str, qlever: QLeverHandler
) -> None:
    async with serve(db_url, QLEVER_URL, qlever) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "failing": ["qlever"],
        "components": {"postgres": "ok", "qlever": "error"},
    }


async def test_ready_names_every_failing_component() -> None:
    async with serve(UNREACHABLE_DATABASE_URL, QLEVER_URL, qlever_refuses) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["failing"] == ["postgres", "qlever"]


async def test_ready_counts_a_slow_component_as_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lae.app, "READY_TIMEOUT_SECONDS", 0.05)

    async def qlever_hangs(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return await qlever_up(request)

    async with serve(UNREACHABLE_DATABASE_URL, QLEVER_URL, qlever_hangs) as client:
        response = await asyncio.wait_for(client.get("/health/ready"), timeout=5)
    assert response.status_code == 503
    assert response.json()["components"]["qlever"] == "error"


async def test_health_endpoints_need_no_authentication() -> None:
    # Probes carry no credentials; neither endpoint may answer 401 or 403.
    async with serve(UNREACHABLE_DATABASE_URL) as client:
        for path in ("/health/live", "/health/ready"):
            response = await client.get(path)
            assert response.status_code not in (401, 403), path
