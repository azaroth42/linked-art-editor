"""The FastAPI application factory for the ``api`` process (``lae api``).

``create_app`` registers the routers from ``lae.api``, the health endpoints, request logging,
authentication (``lae.auth.setup.install_auth``: session cookie, OIDC client, settings checks that
raise ``AuthConfigError``), the handler that turns ``lae.api.errors.ApiError`` into a
structured ``{code, path, params}`` body, and the 404 handler that redirects a request for a
record alias to the record (``not_found_handler``, ADR-0030). Its lifespan owns the two
process-wide resources, both kept on ``app.state``: the SQLAlchemy async engine (with its
session factory, see ``lae.db.session.get_session``) and one ``httpx.AsyncClient`` for outgoing
calls (QLever, ``crdt-ws``).

The built Public Reader script is mounted under ``/reader/assets`` (``lae.reader.assets``).

``openapi_document`` builds the same OpenAPI document as ``GET /openapi.json`` without serving
the application; ``scripts/export_openapi.py`` writes it for the frontend's generated API types.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from fastapi.exception_handlers import http_exception_handler
from pydantic import BaseModel, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.exceptions import HTTPException as StarletteHTTPException

import lae
from lae import api
from lae.api.errors import ApiError, api_error_handler
from lae.api.validation import SNAPSHOT_SOURCE_STATE
from lae.auth.setup import install_auth
from lae.crdt.client import CrdtWsClient
from lae.db.session import create_engine, create_sessionmaker
from lae.logging import RequestContextMiddleware, get_logger
from lae.reader.assets import mount_assets
from lae.settings import Settings

HTTP_TIMEOUT_SECONDS = 30.0
"""Default timeout of the shared HTTP client."""

READY_TIMEOUT_SECONDS = 2.0
"""Time each readiness check may take before its component counts as failing."""

QLEVER_READY_QUERY = "ASK {}"
"""The trivial query ``/health/ready`` sends to QLever."""

ComponentStatus = Literal["ok", "error", "not_configured"]

logger = get_logger(__name__)


class LiveStatus(BaseModel):
    """Body of ``GET /health/live``."""

    status: Literal["ok"] = "ok"


class ReadyStatus(BaseModel):
    """Body of ``GET /health/ready``: overall status, failing components, every component."""

    status: Literal["ok", "unavailable"]
    failing: list[str]
    components: dict[str, ComponentStatus]


def get_app_settings(request: Request) -> Settings:
    """FastAPI dependency: the ``Settings`` the application was created with."""
    settings: Settings = request.app.state.settings
    return settings


def get_engine(request: Request) -> AsyncEngine:
    """FastAPI dependency: the process-wide async engine opened by the lifespan."""
    engine: AsyncEngine = request.app.state.engine
    return engine


def get_http_client(request: Request) -> httpx.AsyncClient:
    """FastAPI dependency: the process-wide ``httpx.AsyncClient`` opened by the lifespan."""
    client: httpx.AsyncClient = request.app.state.http_client
    return client


async def check_postgres(engine: AsyncEngine) -> None:
    """Run ``SELECT 1``; raise if PostgreSQL cannot answer."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def check_qlever(client: httpx.AsyncClient, qlever_url: str) -> None:
    """Send ``ASK {}`` to the QLever query endpoint; raise unless it answers with a boolean."""
    response = await client.get(
        qlever_url,
        params={"query": QLEVER_READY_QUERY},
        headers={"Accept": "application/sparql-results+json"},
        timeout=READY_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    if not isinstance(response.json().get("boolean"), bool):
        raise ValueError("QLever answered ASK without a boolean result")


async def _component_status(name: str, check: Coroutine[Any, Any, None]) -> ComponentStatus:
    """Await one readiness check under the timeout; any failure is logged and reported."""
    try:
        await asyncio.wait_for(check, READY_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("readiness_check_failed", component=name, error=repr(exc))
        return "error"
    return "ok"


health_router = APIRouter(prefix="/health", tags=["health"])


@health_router.get("/live")
async def health_live() -> LiveStatus:
    """Liveness: the process is serving requests. Always 200."""
    return LiveStatus()


@health_router.get(
    "/ready",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyStatus}},
)
async def health_ready(
    response: Response,
    settings: Annotated[Settings, Depends(get_app_settings)],
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> ReadyStatus:
    """Readiness: PostgreSQL and, when ``LAE_QLEVER_URL`` is set, QLever answer; else 503."""
    checks: dict[str, Coroutine[Any, Any, None]] = {"postgres": check_postgres(engine)}
    if settings.qlever_url:
        checks["qlever"] = check_qlever(client, settings.qlever_url)
    results = await asyncio.gather(*(_component_status(n, c) for n, c in checks.items()))
    components: dict[str, ComponentStatus] = dict(zip(checks, results, strict=True))
    components.setdefault("qlever", "not_configured")
    failing = [name for name, result in components.items() if result == "error"]
    if failing:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyStatus(
        status="unavailable" if failing else "ok", failing=failing, components=components
    )


async def not_found_handler(request: Request, exc: Exception) -> Response:
    """Answer a 404 for an unrouted path with a 301 when the path is a record alias (ADR-0030).

    Every other ``HTTPException`` gets FastAPI's default answer.
    """
    from lae.api.public import alias_fallback

    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == status.HTTP_404_NOT_FOUND:
        redirect = await alias_fallback(request)
        if redirect is not None:
            return redirect
    return await http_exception_handler(request, exc)


def create_app(
    settings: Settings, *, http_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """Build the ``api`` application; ``http_transport`` replaces the network in tests.

    Raises ``lae.auth.setup.AuthConfigError`` when the authentication settings must not be served.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Validation's schemas and shapes (Step 1.9), the Public Reader's template and strings
        # (Step 1.21) and the UI Profiles (Step 1.18) are loaded now, so that a broken file stops
        # the start instead of failing the first request.
        from lae.api.profiles import warm_up as warm_up_profiles
        from lae.reader.render import warm_up as warm_up_reader
        from lae.validation import warm_up

        await asyncio.to_thread(warm_up)
        await asyncio.to_thread(warm_up_reader)
        await asyncio.to_thread(warm_up_profiles)
        engine = create_engine(settings.database_url)
        http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, transport=http_transport)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.http_client = http_client
        # Live drafts are read from crdt-ws (decision A4); validation's DraftSnapshotSource.
        setattr(app.state, SNAPSHOT_SOURCE_STATE, CrdtWsClient.from_settings(settings, http_client))
        try:
            yield
        finally:
            await http_client.aclose()
            await engine.dispose()

    app = FastAPI(title="Laertes", version=lae.__version__, lifespan=lifespan)
    app.state.settings = settings
    install_auth(app, settings, http_transport)
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(StarletteHTTPException, not_found_handler)
    app.include_router(health_router)
    for router in api.routers():
        app.include_router(router)
    mount_assets(app, settings)
    return app


def openapi_document() -> dict[str, Any]:
    """Return the ``api`` application's OpenAPI document without serving it or reading ``LAE_*``.

    The routes do not depend on the settings, so the defaults are used; the environment is
    ignored so the document is the same on every machine.
    """
    settings = Settings.model_construct(session_secret=SecretStr("openapi-export"))
    return create_app(settings).openapi()
