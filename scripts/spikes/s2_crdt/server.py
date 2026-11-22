"""Spike S2 server: FastAPI mounting pycrdt-websocket's ASGIServer, in-memory rooms.

One room per WebSocket path: a client connecting to ``ws://host:port/ws/<name>`` joins the
room ``/ws/<name>``. Rooms are kept in memory (``auto_clean_rooms=False``) until evicted through
``POST /rooms/{name}/evict``. ``GET /stats`` reports process RSS, CPU time, event-loop lag,
log error counts and, with ``?detail=1``, each room's clients, size, state vector and content.

With ``--persist-dsn``, every incoming document update (y-sync step 2 or update) is inserted
into Postgres from ``YRoom.on_message``, which pycrdt-websocket awaits *before* applying and
broadcasting the message: the persist-before-broadcast ordering Phase 1 requires (B5).

The guide names the package ``pycrdt_websocket``; from 0.16 it is the namespace package
``pycrdt.websocket`` (same distribution, ``pycrdt-websocket==0.16.9``).

Run (from ``backend/``)::

    uv run --group spike python ../scripts/spikes/s2_crdt/server.py --port 9507
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import logging
import os
import statistics
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException
from pycrdt import Awareness, Doc, Map, Text, YMessageType, YSyncMessageType, read_message
from pycrdt.websocket import ASGIServer, WebsocketServer, YRoom
from s2common import content_digest, decode_state_vector, pct, summarize

log = logging.getLogger("s2.server")
PROCESS = psutil.Process(os.getpid())


class Counters:
    """Connection and error counters exposed through ``/stats``."""

    def __init__(self) -> None:
        self.connects = 0
        self.disconnects = 0
        self.handled_exceptions = 0
        self.log_errors = 0
        self.log_warnings = 0
        self.error_samples: list[str] = []
        self.loop_lag_ms: list[float] = []
        self.persist_ms: list[float] = []
        self.persisted_updates = 0
        self.persisted_bytes = 0

    def reset(self) -> None:
        """Clear the counters (rooms are untouched)."""
        self.__init__()  # type: ignore[misc]


COUNTERS = Counters()


class CountingHandler(logging.Handler):
    """Counts WARNING and ERROR records from pycrdt, uvicorn and this module."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            COUNTERS.log_errors += 1
        elif record.levelno >= logging.WARNING:
            COUNTERS.log_warnings += 1
        if record.levelno >= logging.WARNING and len(COUNTERS.error_samples) < 20:
            COUNTERS.error_samples.append(f"{record.name}: {record.getMessage()}"[:300])


def exception_handler(exception: Exception, logger: logging.Logger) -> bool:
    """Log and swallow room/server exceptions, counting them."""
    COUNTERS.handled_exceptions += 1
    logger.error("WebsocketServer exception: %r", exception)
    return True


class Persistence:
    """Optional synchronous update log in Postgres (table ``s2_crdt_updates``)."""

    def __init__(self) -> None:
        self.dsn: str | None = None
        self.pool: Any = None

    async def start(self) -> None:
        """Open the pool and (re)create the spike table."""
        if not self.dsn:
            return
        import asyncpg

        self.pool = await asyncpg.create_pool(self.dsn, min_size=2, max_size=8)
        await self.pool.execute(
            "CREATE TABLE IF NOT EXISTS s2_crdt_updates (id bigserial PRIMARY KEY, "
            "room text NOT NULL, update bytea NOT NULL, created_at timestamptz DEFAULT now())"
        )
        await self.pool.execute("TRUNCATE s2_crdt_updates")

    async def stop(self) -> None:
        """Close the pool."""
        if self.pool is not None:
            await self.pool.close()

    def hook(self, room_name: str) -> Callable[[bytes], Awaitable[bool]]:
        """Build a ``YRoom.on_message`` callback that persists document updates first."""

        async def on_message(message: bytes) -> bool:
            if (
                self.pool is not None
                and message[0] == YMessageType.SYNC
                and message[1] in (YSyncMessageType.SYNC_STEP2, YSyncMessageType.SYNC_UPDATE)
            ):
                update = read_message(message[2:])
                start = time.perf_counter()
                await self.pool.execute(
                    "INSERT INTO s2_crdt_updates (room, update) VALUES ($1, $2)",
                    room_name,
                    update,
                )
                COUNTERS.persist_ms.append((time.perf_counter() - start) * 1000)
                COUNTERS.persisted_updates += 1
                COUNTERS.persisted_bytes += len(update)
            return False  # never skip: the room applies and broadcasts afterwards

        return on_message


PERSISTENCE = Persistence()


class SpikeWebsocketServer(WebsocketServer):
    """WebsocketServer that installs the persistence hook on every room it creates."""

    async def get_room(self, name: str) -> YRoom:
        """Get or create the room and make sure its ``on_message`` hook is set."""
        room = await super().get_room(name)
        if PERSISTENCE.dsn and room.on_message is None:
            room.on_message = PERSISTENCE.hook(name)
        return room


websocket_server = SpikeWebsocketServer(
    rooms_ready=True,
    auto_clean_rooms=False,
    exception_handler=exception_handler,
    log=log,
)


async def on_connect(message: dict[str, Any], scope: dict[str, Any]) -> bool:
    """Accept every connection (returning True would reject it)."""
    COUNTERS.connects += 1
    return False


async def on_disconnect(message: dict[str, Any]) -> None:
    """Count disconnects."""
    COUNTERS.disconnects += 1


async def loop_lag_monitor(interval: float = 0.05) -> None:
    """Record how late a short sleep wakes up: a direct measure of event-loop saturation."""
    while True:
        start = time.perf_counter()
        await asyncio.sleep(interval)
        COUNTERS.loop_lag_ms.append((time.perf_counter() - start - interval) * 1000)
        if len(COUNTERS.loop_lag_ms) > 20000:
            del COUNTERS.loop_lag_ms[:10000]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the pycrdt WebsocketServer for the lifetime of the app."""
    await PERSISTENCE.start()
    async with websocket_server:
        lag_task = asyncio.create_task(loop_lag_monitor())
        try:
            yield
        finally:
            lag_task.cancel()
            await PERSISTENCE.stop()


app = FastAPI(lifespan=lifespan)
app.mount("/ws", ASGIServer(websocket_server, on_connect=on_connect, on_disconnect=on_disconnect))


def room_content(room: YRoom) -> dict[str, Any]:
    """Project the room document's two spike root types to plain JSON."""
    fields = room.ydoc.get("fields", type=Map).to_py() or {}
    notes = str(room.ydoc.get("notes", type=Text))
    return {"fields": fields, "notes": notes}


@app.get("/stats")
async def stats(
    detail: int = 0, room: str | None = None, content: int = 0, objects: int = 0
) -> dict[str, Any]:
    """Process and room statistics for the load driver."""
    mem = PROCESS.memory_info()
    cpu = PROCESS.cpu_times()
    lag = COUNTERS.loop_lag_ms
    body: dict[str, Any] = {
        "pid": os.getpid(),
        "time": time.time(),
        "rss_bytes": mem.rss,
        "cpu_user_s": cpu.user,
        "cpu_system_s": cpu.system,
        "rooms": len(websocket_server.rooms),
        "clients": sum(len(r.clients) for r in websocket_server.rooms.values()),
        "connects": COUNTERS.connects,
        "disconnects": COUNTERS.disconnects,
        "handled_exceptions": COUNTERS.handled_exceptions,
        "log_errors": COUNTERS.log_errors,
        "log_warnings": COUNTERS.log_warnings,
        "error_samples": COUNTERS.error_samples,
        "loop_lag_ms": {
            "n": len(lag),
            "p50": pct(lag, 50),
            "p99": pct(lag, 99),
            "max": max(lag) if lag else None,
            "mean": statistics.fmean(lag) if lag else None,
        },
        "persist": {
            "enabled": bool(PERSISTENCE.dsn),
            "updates": COUNTERS.persisted_updates,
            "bytes": COUNTERS.persisted_bytes,
            "ms": summarize(COUNTERS.persist_ms),
        },
    }
    if objects:
        # Leak check: live pycrdt objects after a full collection.
        gc.collect()
        live = gc.get_objects()
        body["live_objects"] = {
            "YRoom": sum(1 for o in live if isinstance(o, YRoom)),
            "Doc": sum(1 for o in live if isinstance(o, Doc)),
            "Awareness": sum(1 for o in live if isinstance(o, Awareness)),
        }
    if detail or room:
        rooms: dict[str, Any] = {}
        for name, yroom in websocket_server.rooms.items():
            if room and name != room:
                continue
            projected = room_content(yroom)
            entry: dict[str, Any] = {
                "clients": len(yroom.clients),
                "update_bytes": len(yroom.ydoc.get_update()),
                "state_vector": decode_state_vector(yroom.ydoc.get_state()),
                "digest": content_digest(projected),
                "awareness_states": len(yroom.awareness.states),
            }
            if content:
                entry["content"] = projected
            rooms[name] = entry
        body["room_detail"] = rooms
    return body


@app.post("/stats/reset")
async def stats_reset() -> dict[str, bool]:
    """Reset counters and loop-lag samples."""
    COUNTERS.reset()
    return {"reset": True}


@app.post("/gc")
async def collect() -> dict[str, int]:
    """Run a full garbage collection so RSS samples are comparable."""
    return {"collected": gc.collect(), "rss_bytes": PROCESS.memory_info().rss}


@app.post("/rooms/{name:path}/evict")
async def evict(name: str) -> dict[str, Any]:
    """Stop and drop a room from memory, as idle eviction will in Phase 1."""
    key = name if name.startswith("/") else f"/{name}"
    yroom = websocket_server.rooms.get(key)
    if yroom is None:
        raise HTTPException(status_code=404, detail="no such room")
    clients = len(yroom.clients)
    await websocket_server.delete_room(name=key)
    return {"evicted": key, "connected_clients_at_eviction": clients}


@app.post("/rooms/evict-all")
async def evict_all() -> dict[str, int]:
    """Evict every room."""
    names = list(websocket_server.rooms)
    for key in names:
        await websocket_server.delete_room(name=key)
    return {"evicted": len(names)}


def main() -> None:
    """Parse arguments and serve with uvicorn (single process, single event loop)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9507)
    parser.add_argument("--log-level", default="warning")
    parser.add_argument("--ws", default="auto", help="uvicorn WebSocket implementation")
    parser.add_argument("--persist-dsn", default=None, help="e.g. postgresql:///lae_i7")
    args = parser.parse_args()
    PERSISTENCE.dsn = args.persist_dsn
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger().addHandler(CountingHandler())
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level, ws=args.ws)


if __name__ == "__main__":
    main()
