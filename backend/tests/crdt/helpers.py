"""Test harness for ``lae.crdt``: records, users, a real server on a port, and a y-sync client.

``YClient`` speaks y-sync and awareness over a real WebSocket (``websockets``) with its own
``pycrdt.Doc``, the way ``y-websocket`` does in a browser: it sends sync step 1 on connect,
answers the server's step 1 with step 2, sends every local change as an update and applies what
it receives. ``running_server`` runs the real ASGI application under uvicorn on a free port.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import itsdangerous
import uvicorn
from pycrdt import (
    Doc,
    YMessageType,
    YSyncMessageType,
    create_awareness_message,
    create_sync_message,
    create_update_message,
    handle_sync_message,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from uuid_utils.compat import uuid7
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from lae.crdt.protocol import decode_awareness, encode_awareness, frame_payload
from lae.crdt.rooms import InMemoryRoomManager
from lae.crdt.server import create_crdt_app
from lae.db.models import CrdtDraft, CrdtUpdate, Record, RecordStatus
from lae.ids import record_uri
from lae.projection import project
from lae.settings import Settings

INTERNAL_TOKEN = "test-internal-token"
SESSION_SECRET = "test-session-secret"
BASE_URI = "http://localhost:8000/id/"


def sample_record(uri: str) -> dict[str, Any]:
    """A small ``HumanMadeObject`` with a name and a statement (a ``Text``).

    Its ``_label`` is the one derived from its name (Step 1.19a), so a snapshot of the draft
    equals the projection of its document until the first name changes.
    """
    return {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": uri,
        "type": "HumanMadeObject",
        "_label": "First name",
        "identified_by": [{"type": "Name", "content": "First name"}],
        "referred_to_by": [{"type": "LinguisticObject", "content": "A statement"}],
    }


AAT = "http://vocab.getty.edu/aat/"
PRIMARY_NAME = {"id": f"{AAT}300404670", "type": "Type"}
ENGLISH = {"id": f"{AAT}300388277", "type": "Language"}
FRENCH = {"id": f"{AAT}300388306", "type": "Language"}


def bilingual_record(uri: str) -> dict[str, Any]:
    """A ``HumanMadeObject`` with an English and a French primary name, English first, and a
    stale stored ``_label`` (Step 1.19a: a snapshot derives it in the room's label language)."""
    return {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": uri,
        "type": "HumanMadeObject",
        "_label": "Stale label",
        "identified_by": [
            {
                "type": "Name",
                "content": "The Night Watch",
                "classified_as": [PRIMARY_NAME],
                "language": [ENGLISH],
            },
            {
                "type": "Name",
                "content": "La Ronde de nuit",
                "classified_as": [PRIMARY_NAME],
                "language": [FRENCH],
            },
        ],
    }


def sessions_of(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory on the test engine."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_record(
    engine: AsyncEngine,
    *,
    version: int = 1,
    draft_epoch: int = 0,
    status: RecordStatus = RecordStatus.ACTIVE,
    data: Callable[[str], dict[str, Any]] = sample_record,
) -> tuple[UUID, str]:
    """Insert a record (committed) and return ``(id, uri)``; ``data(uri)`` is its JSON-LD."""
    record_id = uuid7()
    uri = record_uri(record_id, base_uri=BASE_URI)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        record = Record(
            id=record_id,
            uri=uri,
            type="HumanMadeObject",
            etag="W/1",
            data=data(uri),
            context_version=1,
        )
        record.version = version
        record.draft_epoch = draft_epoch
        record.status = status
        session.add(record)
        await session.commit()
    return record_id, uri


async def record_epoch(engine: AsyncEngine, record_id: UUID) -> int | None:
    """``records.draft_epoch`` of a record."""
    async with AsyncSession(engine) as session:
        epoch: int | None = await session.scalar(
            select(Record.draft_epoch).where(Record.id == record_id)
        )
        return epoch


async def draft_row(engine: AsyncEngine, record_id: UUID) -> CrdtDraft | None:
    """The record's ``crdt_drafts`` row, if any."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await session.get(CrdtDraft, record_id)


async def update_rows(engine: AsyncEngine, record_id: UUID) -> list[bytes]:
    """The record's ``crdt_updates`` payloads, oldest first."""
    async with AsyncSession(engine) as session:
        rows = await session.scalars(
            select(CrdtUpdate.update)
            .where(CrdtUpdate.record_id == record_id)
            .order_by(CrdtUpdate.id)
        )
        return [bytes(row) for row in rows]


async def update_count(engine: AsyncEngine, record_id: UUID) -> int:
    """Number of ``crdt_updates`` rows of a record."""
    async with AsyncSession(engine) as session:
        count = await session.scalar(
            select(func.count()).select_from(CrdtUpdate).where(CrdtUpdate.record_id == record_id)
        )
        return int(count or 0)


def replica_of(doc: Doc[Any]) -> Doc[Any]:
    """A second replica holding the same state as ``doc``."""
    replica: Doc[Any] = Doc()
    replica.apply_update(doc.get_update())
    return replica


def change(replica: Doc[Any], edit: Callable[[Doc[Any]], object]) -> bytes:
    """Run ``edit`` on ``replica`` and return the update it produced."""
    before = replica.get_state()
    edit(replica)
    return replica.get_update(before)


async def crash(manager: InMemoryRoomManager) -> None:
    """Stop a manager the way a killed process stops: nothing is compacted or closed cleanly."""
    sweeper, manager._sweeper = manager._sweeper, None
    if sweeper is not None:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
    for room in list(manager._rooms.values()):
        room.closed = True
        await room.shutdown()
    manager._rooms.clear()


async def eventually(
    predicate: Callable[[], bool | Awaitable[bool]], *, seconds: float = 5.0, what: str = ""
) -> None:
    """Poll ``predicate`` until it holds; fail after ``seconds``."""
    deadline = time.monotonic() + seconds
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"not reached within {seconds}s: {what or predicate}")
        await asyncio.sleep(0.02)


def free_port() -> int:
    """A TCP port nothing listens on (bound and released; fine for a single test process)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def crdt_settings(database_url: str, **overrides: Any) -> Settings:
    """Settings for a test ``crdt-ws``: no dev user, known secrets."""
    values: dict[str, Any] = {
        "database_url": database_url,
        "env": "test",
        "internal_token": INTERNAL_TOKEN,
        "session_secret": SESSION_SECRET,
        "dev_user": None,
        "oidc_issuer": None,
        "qlever_url": None,
    }
    values.update(overrides)
    return Settings(**values)


def session_cookie(user_id: UUID, secret: str = SESSION_SECRET) -> str:
    """A ``lae_session`` cookie value as ``api`` (Starlette ``SessionMiddleware``) issues it."""
    payload = base64.b64encode(
        json.dumps({"user_id": str(user_id), "iat": int(time.time())}).encode()
    )
    return itsdangerous.TimestampSigner(secret).sign(payload).decode()


@dataclass
class RunningServer:
    """A ``crdt-ws`` application served by uvicorn on ``127.0.0.1:port``."""

    port: int
    app: Any

    @property
    def manager(self) -> InMemoryRoomManager:
        manager: InMemoryRoomManager = self.app.state.rooms
        return manager

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def ws_url(self, record_id: object, **params: object) -> str:
        query = "&".join(f"{key}={value}" for key, value in params.items())
        return f"ws://127.0.0.1:{self.port}/ws/rooms/{record_id}" + (f"?{query}" if query else "")


@asynccontextmanager
async def running_server(
    settings: Settings, *, port: int | None = None
) -> AsyncIterator[RunningServer]:
    """Serve ``create_crdt_app(settings)`` for real; stop it (gracefully) on exit."""
    app = create_crdt_app(settings)
    port = port or free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        await eventually(lambda: server.started, what="uvicorn started")
        yield RunningServer(port, app)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 20)


class YClient:
    """A minimal y-websocket client: one ``Doc``, y-sync, awareness."""

    def __init__(self, url: str, *, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = headers or {}
        self.doc: Doc[Any] = Doc()
        self.synced = asyncio.Event()
        self.presence: dict[int, dict[str, Any]] = {}
        """Awareness states received, by client id (removed states are deleted)."""
        self.removed: list[int] = []
        self.updates_received = 0
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self.closed = asyncio.Event()
        self._clock = 0
        self._remote = False
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._sends: list[asyncio.Task[None]] = []
        self.doc.observe(self._on_change)

    async def __aenter__(self) -> YClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the socket and start the sync handshake."""
        self.synced.clear()
        self.closed.clear()
        self.close_code = None
        self._ws = await connect(self.url, additional_headers=self.headers)
        self._reader = asyncio.create_task(self._read(self._ws))
        with suppress(ConnectionClosed):  # the server may already have refused us
            await self._ws.send(create_sync_message(self.doc))

    async def _read(self, ws: ClientConnection) -> None:
        try:
            async for message in ws:
                assert isinstance(message, bytes)
                self._receive(ws, message)
        except ConnectionClosed:
            pass
        finally:
            self.close_code = ws.close_code
            self.close_reason = ws.close_reason
            self.closed.set()

    def _receive(self, ws: ClientConnection, message: bytes) -> None:
        if message[0] == YMessageType.SYNC:
            if message[1] == YSyncMessageType.SYNC_STEP1:
                reply = handle_sync_message(message[1:], self.doc)
                assert reply is not None
                self._send(ws, reply)
                return
            self._remote = True
            try:
                handle_sync_message(message[1:], self.doc)
            finally:
                self._remote = False
            if message[1] == YSyncMessageType.SYNC_STEP2:
                self.synced.set()
            else:
                self.updates_received += 1
        elif message[0] == YMessageType.AWARENESS:
            for client_id, _clock, state in decode_awareness(frame_payload(message[1:])):
                if state is None:
                    self.presence.pop(client_id, None)
                    self.removed.append(client_id)
                else:
                    self.presence[client_id] = state

    def _send(self, ws: ClientConnection, message: bytes) -> None:
        async def send() -> None:
            with suppress(ConnectionClosed):
                await ws.send(message)

        self._sends.append(asyncio.create_task(send()))

    def _on_change(self, event: Any) -> None:
        if not self._remote and self._ws is not None and not self.closed.is_set():
            self._send(self._ws, create_update_message(event.update))

    async def wait_synced(self, seconds: float = 5.0) -> None:
        """Wait for the server's sync step 2."""
        await asyncio.wait_for(self.synced.wait(), seconds)

    async def wait_closed(self, seconds: float = 5.0) -> tuple[int | None, str | None]:
        """Wait for the socket to close; return ``(code, reason)``."""
        await asyncio.wait_for(self.closed.wait(), seconds)
        return self.close_code, self.close_reason

    def project(self) -> dict[str, Any]:
        """The client's document as JSON-LD."""
        return project(self.doc)

    async def set_presence(self, state: dict[str, Any] | None) -> None:
        """Publish this client's awareness state (``None`` leaves)."""
        assert self._ws is not None
        self._clock += 1
        payload = encode_awareness([(self.doc.client_id, self._clock, state)])
        await self._ws.send(create_awareness_message(payload))

    async def send_raw(self, message: bytes) -> None:
        """Send one frame as is."""
        assert self._ws is not None
        await self._ws.send(message)

    async def close(self) -> None:
        """Close the socket gracefully."""
        if self._ws is not None:
            await self._ws.close()
        await self._finish()

    async def abort(self) -> None:
        """Drop the TCP connection without a close frame (a crashed tab)."""
        if self._ws is not None:
            self._ws.transport.abort()
        await self._finish()

    async def _finish(self) -> None:
        if self._reader is not None:
            with suppress(Exception):
                await asyncio.wait_for(self._reader, 5)
        for task in self._sends:
            task.cancel()
        self._ws = None
