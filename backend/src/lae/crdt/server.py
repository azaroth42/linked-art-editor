"""The ``crdt-ws`` process: WebSocket rooms and the internal RPC, in one ASGI application.

``lae crdt-ws`` runs ``main(settings)``; ``create_crdt_app`` builds the application, whose
lifespan owns the database engine and the one ``InMemoryRoomManager`` of the deployment
(decision A4). It listens on ``LAE_CRDT_WS_HOST``:``LAE_CRDT_WS_PORT``.

WebSocket ``/ws/rooms/{record_id}`` (y-sync and awareness, as spoken by ``y-websocket``)
-----------------------------------------------------------------------------------

**Origin.** A browser sends its session cookie with a WebSocket handshake to this server from
any page, whatever site the page came from, and a WebSocket is not subject to the same-origin
policy. Without a check, a page on another origin could open a room as the signed-in user and
read and write the draft (cross-site WebSocket hijacking). So a handshake that carries an
``Origin`` header is refused with HTTP 403, before anything else, unless the origin is allowed
(``allowed_origins``): the origin of ``LAE_BASE_URI``, every entry of ``LAE_ALLOWED_ORIGINS``,
and in ``LAE_ENV=development`` any loopback origin (the Vite dev server). A handshake without
``Origin`` is not from a browser (a script, an agent, ``y-websocket`` in Node) and is let
through to authentication; such a client has no ambient cookie to abuse.

After that the handshake is always accepted and a refusal is a close frame, because a close code
can only be sent on an open socket (S2 finding 8). In order:

1. ``record_id`` must be a UUID, else close ``4400 bad-request``.
2. Authentication: ``?token=<PAT>`` or ``Authorization: Bearer`` (``lae.auth.tokens``), else
   ``LAE_DEV_USER``, else the ``lae_session`` cookie (same ``LAE_SESSION_SECRET`` as ``api``).
   Failing that, close ``4401 unauthenticated``. A bad token never falls back to the cookie.
3. The client states the draft epoch it holds: ``?epoch=<n>`` (browsers cannot set headers on
   a WebSocket; ``y-websocket`` sends ``params``) or the header ``X-Draft-Epoch``. Missing or
   not a number: close ``4400 bad-request``. A client that cannot say which draft its document
   belongs to cannot be let in, because its document would be merged into the room.
4. The room is opened. A caller with ``edit_draft`` creates the draft if the record has none.
   A caller without it (a Viewer) is **read-only**: it joins an existing draft or is closed with
   ``4404 not-found``; its document updates are dropped; its user id is listed in the server's
   awareness state (``lae.crdt.rooms``). Unknown record: ``4404 not-found``. Deleted or merged
   record: ``4410 not-editable``.
5. Epoch guard (decision B6): if the stated epoch is not the record's current draft epoch,
   close ``4409 stale-epoch``; no draft is created for such a client. The client keeps its
   document and reloads (Step 1.17).

While connected, the server may close with ``4409 stale-epoch`` (the draft was published or
discarded), ``1012 room-closed`` (the room left memory or the server is restarting: reconnect),
``4403 ejected``, ``4400 protocol-error`` or ``1011 persist-failed`` (reconnect and resync).
``y-websocket`` treats 4400-4499 as final and reconnects after any other code.

Internal RPC ``/internal/...`` (ADR-0011; header ``X-Internal-Token: <LAE_INTERNAL_TOKEN>``)
-----------------------------------------------------------------------------------------

Called by ``api`` through ``lae.crdt.client.CrdtWsClient``. Without the right token every
endpoint answers 401 ``LAE-CRDT-001``; a server without ``LAE_INTERNAL_TOKEN`` refuses them all.

- ``GET  /internal/rooms`` → ``[RoomInfo]``
- ``GET  /internal/rooms/{id}/snapshot`` → ``Snapshot``; 404 ``LAE-CRDT-003`` without a draft
- ``POST /internal/rooms/{id}/open`` body ``{content?, created_by?}`` → ``OpenedRoom``
- ``POST /internal/rooms/{id}/close`` body ``{discard, draft_epoch?}`` → 204
- ``POST /internal/rooms/{id}/apply`` body ``{update: base64, user_id}`` → 204
- ``POST /internal/users/{id}/eject`` → ``{closed}``
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hmac
import logging
import re
import signal
import sys
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any, Final, cast
from urllib.parse import urlsplit
from uuid import UUID

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Request, Response, WebSocket, status
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import lae
from lae.api.errors import ApiError, api_error_handler
from lae.auth import dev, oidc, tokens
from lae.auth.permissions import CurrentUser
from lae.auth.setup import AuthConfigError, check_auth_settings, session_secret
from lae.crdt import errors
from lae.crdt.instance import InstanceLock
from lae.crdt.rooms import (
    CLOSE_ROOM_CLOSED,
    CLOSE_STALE_EPOCH,
    REASON_ROOM_CLOSED,
    REASON_STALE_EPOCH,
    Connection,
    DraftExistsError,
    DraftNotFoundError,
    DraftSupersededError,
    EpochMismatchError,
    InMemoryRoomManager,
    InvalidUpdateError,
    RecordNotEditableError,
    RecordNotFoundError,
    RoomClosedError,
    RoomFailedError,
    RoomInfo,
    Snapshot,
)
from lae.db.session import create_engine, create_sessionmaker
from lae.logging import RequestContextMiddleware, get_logger
from lae.projection import ProjectionError
from lae.settings import Settings

INTERNAL_TOKEN_HEADER: Final = "X-Internal-Token"
EPOCH_HEADER: Final = "X-Draft-Epoch"
EPOCH_PARAM: Final = "epoch"
TOKEN_PARAM: Final = "token"

EDIT_PERMISSION: Final = "edit_draft"

CLOSE_BAD_REQUEST: Final = 4400
CLOSE_UNAUTHENTICATED: Final = 4401
CLOSE_NOT_FOUND: Final = 4404
CLOSE_NOT_EDITABLE: Final = 4410

TOKEN_REQUIRED_ENVS: Final = frozenset({"staging", "production"})
"""Environments in which ``LAE_INTERNAL_TOKEN`` must be set for ``crdt-ws`` to start."""

EXIT_CONFIG_ERROR: Final = 2

LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_PORTS: Final = {"http": 80, "https": 443}

logger = get_logger(__name__)


class RedactTokens(logging.Filter):
    """Logging filter: replace the value of ``token=`` in a message with ``[redacted]``.

    uvicorn logs the full URL of every WebSocket it accepts, and browsers can only send a
    Personal Access Token in the query string (conventions §9: no secrets in logs).
    """

    PATTERN: Final = re.compile(r"(?i)\b(token=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the record in place; never drop it."""
        message = record.getMessage()
        if "token=" in message.lower():
            record.msg = self.PATTERN.sub(r"\1[redacted]", message)
            record.args = None
        return True


def redact_tokens_in_server_logs() -> None:
    """Install :class:`RedactTokens` on the uvicorn loggers (idempotent)."""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        server_logger = logging.getLogger(name)
        if not any(isinstance(f, RedactTokens) for f in server_logger.filters):
            server_logger.addFilter(RedactTokens())


class OpenBody(BaseModel):
    """Body of ``POST /internal/rooms/{id}/open``."""

    content: dict[str, Any] | None = None
    """JSON-LD to hydrate a **new** draft from instead of ``records.data``."""
    created_by: UUID | None = None


class OpenedRoom(BaseModel):
    """Response of ``POST /internal/rooms/{id}/open``."""

    record_id: UUID
    draft_epoch: int
    base_version: int


class CloseBody(BaseModel):
    """Body of ``POST /internal/rooms/{id}/close``."""

    discard: bool
    draft_epoch: int | None = None
    """With ``discard``: only the draft of this epoch (see ``InMemoryRoomManager.close``)."""


class ApplyBody(BaseModel):
    """Body of ``POST /internal/rooms/{id}/apply``."""

    update: str
    """A Yjs update (v1), base64."""
    user_id: UUID | None = None
    """The user credited as contributor; ``None`` for a change no user made."""


class Ejected(BaseModel):
    """Response of ``POST /internal/users/{id}/eject``."""

    closed: int


class WebSocketChannel:
    """A ``pycrdt.Channel`` over a Starlette ``WebSocket`` (binary frames only)."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self.gone = False
        """True once the peer has disconnected or a send has failed."""

    @property
    def path(self) -> str:
        """The request path, as ``pycrdt`` logs it."""
        return str(self._websocket.url.path)

    def __aiter__(self) -> WebSocketChannel:
        return self

    async def __anext__(self) -> bytes:
        return await self.recv()

    async def send(self, message: bytes) -> None:
        """Send one binary frame; a socket that has gone away swallows it."""
        if self.gone:
            return
        try:
            await self._websocket.send_bytes(message)
        except Exception:
            self.gone = True

    async def recv(self) -> bytes:
        """Return the next binary frame (``b""`` for a text frame); stop when the peer leaves."""
        message = await self._websocket.receive()
        if message["type"] == "websocket.disconnect":
            self.gone = True
            raise StopAsyncIteration
        return message.get("bytes") or b""


async def authenticate_websocket(websocket: WebSocket, settings: Settings) -> CurrentUser | None:
    """Resolve the caller of a WebSocket: token, else dev user, else session cookie."""
    raw_token = websocket.query_params.get(TOKEN_PARAM)
    if raw_token is None:
        scheme, _, credentials = websocket.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            raw_token = credentials.strip()
    # The auth helpers are typed for Request; they use only scope, session and app, which a
    # WebSocket has too.
    request = cast(Request, websocket)
    async with websocket.app.state.sessionmaker() as session:
        if raw_token is not None:
            return await tokens.authenticate_token(session, raw_token)
        if settings.dev_user:
            return await dev.dev_principal(request, session, settings.dev_user)
        return await oidc.session_principal(request, session)


def normalize_origin(value: str) -> str | None:
    """``scheme://host[:port]`` in lower case without a default port; ``None`` if not an origin.

    Accepts a URL too (its path is ignored), so that ``LAE_BASE_URI`` can be passed.
    """
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    if scheme not in DEFAULT_PORTS or not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    return (
        f"{scheme}://{host}"
        if port in (None, DEFAULT_PORTS[scheme])
        else f"{scheme}://{host}:{port}"
    )


def allowed_origins(settings: Settings) -> frozenset[str]:
    """The origins a browser may open a room from: that of ``base_uri`` and ``allowed_origins``."""
    candidates = [settings.base_uri, *settings.allowed_origins]
    return frozenset(origin for origin in map(normalize_origin, candidates) if origin is not None)


def origin_allowed(origin: str | None, settings: Settings) -> bool:
    """Whether a handshake with this ``Origin`` header may proceed (see the module docstring)."""
    if origin is None:
        return True  # not a browser
    normalized = normalize_origin(origin)
    if normalized is None:
        return False  # "null" (sandboxed frame, file:) or anything that is not http(s)
    if normalized in allowed_origins(settings):
        return True
    host = urlsplit(normalized).hostname
    return settings.env == "development" and host in LOOPBACK_HOSTS


def stated_epoch(websocket: WebSocket) -> int | None:
    """The draft epoch the client says it holds, or ``None`` if absent or not a number."""
    raw = websocket.headers.get(EPOCH_HEADER) or websocket.query_params.get(EPOCH_PARAM)
    if raw is None or not raw.strip().isascii() or not raw.strip().isdigit():
        return None
    return int(raw.strip())


async def serve_room(websocket: WebSocket, record_id: str) -> None:
    """The ``/ws/rooms/{record_id}`` endpoint (see the module docstring for the steps)."""
    manager: InMemoryRoomManager = websocket.app.state.rooms
    settings: Settings = websocket.app.state.settings
    origin = websocket.headers.get("origin")
    if not origin_allowed(origin, settings):
        logger.warning("ws_origin_refused", record_id=record_id, origin=(origin or "")[:200])
        await websocket.close()  # before accept: the handshake is answered with HTTP 403
        return
    await websocket.accept()

    async def refuse(code: int, reason: str) -> None:
        logger.info("ws_refused", record_id=record_id, code=code, reason=reason)
        with contextlib.suppress(Exception):
            await websocket.close(code=code, reason=reason)

    try:
        room_id = UUID(record_id)
    except ValueError:
        return await refuse(CLOSE_BAD_REQUEST, "bad-request")
    user = await authenticate_websocket(websocket, settings)
    if user is None:
        return await refuse(CLOSE_UNAUTHENTICATED, "unauthenticated")
    epoch = stated_epoch(websocket)
    if epoch is None:
        return await refuse(CLOSE_BAD_REQUEST, "bad-request")
    read_only = not user.has(EDIT_PERMISSION)
    try:
        room = await manager.open(
            room_id, create=not read_only, created_by=user.id, expected_epoch=epoch
        )
    except EpochMismatchError:
        return await refuse(CLOSE_STALE_EPOCH, REASON_STALE_EPOCH)
    except (RecordNotFoundError, DraftNotFoundError):
        return await refuse(CLOSE_NOT_FOUND, "not-found")
    except (RecordNotEditableError, ProjectionError):
        return await refuse(CLOSE_NOT_EDITABLE, "not-editable")
    except RoomFailedError:
        return await refuse(CLOSE_ROOM_CLOSED, REASON_ROOM_CLOSED)
    if epoch != room.draft_epoch:  # cannot happen: open has checked it
        return await refuse(CLOSE_STALE_EPOCH, REASON_STALE_EPOCH)
    channel = WebSocketChannel(websocket)
    connection = Connection(
        channel=channel,
        user_id=user.id,
        display_name=user.display_name,
        kind=user.kind.value,
        read_only=read_only,
    )
    logger.info(
        "ws_joined", record_id=record_id, user_id=str(user.id), read_only=read_only, epoch=epoch
    )
    try:
        await manager.serve(room, connection)
    finally:
        logger.info(
            "ws_left",
            record_id=record_id,
            user_id=str(user.id),
            code=connection.close_code,
            reason=connection.close_reason,
            dropped_updates=connection.dropped_updates,
        )
        if connection.close_code is not None and not channel.gone:
            with contextlib.suppress(Exception):
                await websocket.close(code=connection.close_code, reason=connection.close_reason)


def require_internal_token(request: Request) -> None:
    """Dependency of every ``/internal/`` endpoint: check ``X-Internal-Token``."""
    settings: Settings = request.app.state.settings
    expected = settings.internal_token
    given = request.headers.get(INTERNAL_TOKEN_HEADER, "")
    if expected is None or not hmac.compare_digest(
        given.encode("utf-8"), expected.get_secret_value().encode("utf-8")
    ):
        raise ApiError(status.HTTP_401_UNAUTHORIZED, errors.INTERNAL_TOKEN_REQUIRED)


def get_rooms(request: Request) -> InMemoryRoomManager:
    """Dependency: the process-wide room manager."""
    manager: InMemoryRoomManager = request.app.state.rooms
    return manager


Rooms = Annotated[InMemoryRoomManager, Depends(get_rooms)]

internal_router = APIRouter(
    prefix="/internal", tags=["internal"], dependencies=[Depends(require_internal_token)]
)


def _room_error(exc: Exception, record_id: UUID) -> ApiError:
    """The structured error for an exception a ``RoomManager`` call raised."""
    params: dict[str, Any] = {"record_id": str(record_id)}
    if isinstance(exc, RecordNotFoundError):
        return ApiError(status.HTTP_404_NOT_FOUND, errors.RECORD_NOT_FOUND, params)
    if isinstance(exc, DraftNotFoundError):
        return ApiError(status.HTTP_404_NOT_FOUND, errors.DRAFT_NOT_FOUND, params)
    if isinstance(exc, DraftExistsError):
        return ApiError(status.HTTP_409_CONFLICT, errors.DRAFT_EXISTS, params)
    if isinstance(exc, RecordNotEditableError):
        return ApiError(status.HTTP_409_CONFLICT, errors.RECORD_NOT_EDITABLE, params)
    if isinstance(exc, InvalidUpdateError):
        return ApiError(422, errors.INVALID_UPDATE, params)
    if isinstance(exc, DraftSupersededError | RoomClosedError):
        return ApiError(status.HTTP_409_CONFLICT, errors.DRAFT_SUPERSEDED, params)
    if isinstance(exc, ProjectionError):
        return ApiError(422, errors.INVALID_CONTENT, {**params, "projection_code": exc.code})
    if isinstance(exc, RoomFailedError):
        return ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, errors.CRDT_WS_UNAVAILABLE, {"error": "room"}
        )
    raise exc


ROOM_ERRORS: Final = (
    RecordNotFoundError,
    DraftNotFoundError,
    DraftExistsError,
    RecordNotEditableError,
    InvalidUpdateError,
    DraftSupersededError,
    RoomClosedError,
    ProjectionError,
    RoomFailedError,
)


@internal_router.get("/rooms")
async def list_rooms(rooms: Rooms) -> list[RoomInfo]:
    """List the rooms in memory."""
    return await rooms.list_rooms()


@internal_router.get("/rooms/{record_id}/snapshot")
async def snapshot(record_id: UUID, rooms: Rooms) -> Snapshot:
    """Project the record's draft (with internal node IRIs); 404 if it has none."""
    try:
        return await rooms.snapshot(record_id)
    except ROOM_ERRORS as exc:
        raise _room_error(exc, record_id) from exc


@internal_router.post("/rooms/{record_id}/open")
async def open_room(record_id: UUID, body: OpenBody, rooms: Rooms) -> OpenedRoom:
    """Open the record's room, creating its draft if it has none."""
    try:
        room = await rooms.open(record_id, content=body.content, created_by=body.created_by)
    except ROOM_ERRORS as exc:
        raise _room_error(exc, record_id) from exc
    return OpenedRoom(
        record_id=record_id, draft_epoch=room.draft_epoch, base_version=room.base_version
    )


@internal_router.post("/rooms/{record_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_room(record_id: UUID, body: CloseBody, rooms: Rooms) -> Response:
    """Drop the room from memory; with ``discard`` delete the draft and end its epoch."""
    await rooms.close(record_id, discard=body.discard, draft_epoch=body.draft_epoch)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@internal_router.post("/rooms/{record_id}/apply", status_code=status.HTTP_204_NO_CONTENT)
async def apply_update(record_id: UUID, body: ApplyBody, rooms: Rooms) -> Response:
    """Persist, apply and broadcast one Yjs update."""
    try:
        update = base64.b64decode(body.update, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _room_error(InvalidUpdateError("not base64"), record_id) from exc
    try:
        await rooms.apply_update(record_id, update, body.user_id)
    except ROOM_ERRORS as exc:
        raise _room_error(exc, record_id) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@internal_router.post("/users/{user_id}/eject")
async def eject_user(user_id: UUID, rooms: Rooms) -> Ejected:
    """Close every socket of a user (admin suspension, Phase 4)."""
    return Ejected(closed=await rooms.eject(user_id))


health_router = APIRouter(prefix="/health", tags=["health"])


@health_router.get("/live")
async def health_live() -> dict[str, str]:
    """Liveness: the process is serving requests."""
    return {"status": "ok"}


def terminate_process() -> None:
    """Ask this process to stop (uvicorn shuts down gracefully on ``SIGTERM``)."""
    signal.raise_signal(signal.SIGTERM)


def create_crdt_app(
    settings: Settings,
    *,
    manager_class: type[InMemoryRoomManager] = InMemoryRoomManager,
    instance_lock_seconds: float | None = None,
    terminate: Callable[[], None] = terminate_process,
) -> FastAPI:
    """Build the ``crdt-ws`` application.

    Only tests pass the keyword arguments: ``manager_class``, ``instance_lock_seconds`` (how
    long to wait for another process's instance lock) and ``terminate`` (what to do when the
    instance lock is lost, after the rooms have been dropped).

    Raises ``lae.auth.setup.AuthConfigError`` when the authentication settings must not be
    served (the same checks as ``api``). The lifespan raises
    ``lae.crdt.instance.InstanceLockError`` when another ``crdt-ws`` serves the database.
    """
    check_auth_settings(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        sessionmaker = create_sessionmaker(engine)
        manager = manager_class(sessionmaker, idle_seconds=float(settings.crdt_room_idle_seconds))

        async def lock_lost() -> None:
            await manager.abandon()
            terminate()

        lock_options = (
            {} if instance_lock_seconds is None else {"acquire_seconds": instance_lock_seconds}
        )
        lock = InstanceLock(engine, on_lost=lock_lost, **lock_options)
        app.state.engine = engine
        app.state.sessionmaker = sessionmaker
        app.state.rooms = manager
        app.state.instance_lock = lock
        try:
            # One crdt-ws per database (decision A4): taken before any room can be opened.
            await lock.acquire()
            await manager.start()
            try:
                yield
            finally:
                await manager.stop()
        finally:
            await lock.release()
            await engine.dispose()

    app = FastAPI(title="Laertes: crdt-ws", version=lae.__version__, lifespan=lifespan)
    app.state.settings = settings
    dev.init_dev_state(app.state)
    # The same cookie as api (lae.auth.setup.install_auth), so a browser session authenticates.
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret(settings),
        session_cookie=oidc.SESSION_COOKIE,
        max_age=oidc.SESSION_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=settings.env != "development",
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(health_router)
    app.include_router(internal_router)
    app.add_api_websocket_route("/ws/rooms/{record_id}", serve_room)
    return app


def main(settings: Settings) -> int:
    """``lae crdt-ws``: serve the application with uvicorn until interrupted."""
    if settings.internal_token is None and settings.env in TOKEN_REQUIRED_ENVS:
        print(
            f"lae crdt-ws: refusing to start: LAE_INTERNAL_TOKEN is required when "
            f"LAE_ENV={settings.env}",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR
    try:
        app = create_crdt_app(settings)
    except AuthConfigError as exc:
        print(f"lae crdt-ws: refusing to start: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    redact_tokens_in_server_logs()
    uvicorn.run(
        app,
        host=settings.crdt_ws_host,
        port=settings.crdt_ws_port,
        log_config=None,  # keep the JSON logging lae.cli configured
        log_level=settings.log_level,
        access_log=False,
        server_header=False,
    )
    return 0
