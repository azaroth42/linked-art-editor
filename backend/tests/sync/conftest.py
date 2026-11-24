"""Fixtures and helpers for the sync worker tests (Phase 1 Step 1.11).

Records are published through the real stack (``served`` and ``network`` from
``tests.api.conftest``, the helpers of ``tests.publish.conftest``), so the outbox events and
revisions are exactly what publish writes; a test inserts rows directly only where publish cannot
produce them (a ``delete`` event, a corrupt revision, a dead letter set by hand).

``queue`` empties ``outbox_events`` and removes ``qlever_index_watermark`` before the test and
puts back every ``sync_state`` row it found afterwards: the worker reads the whole queue, and the
backend suite runs serially on one database.

``RecordingClient`` is a ``QLeverClient`` whose ``update`` records the text, then does what its
``behaviour`` says (by default: apply the update to a ``pyoxigraph.Store``, which stands in for
QLever's state); ``acknowledged`` lists the updates that did not raise. A behaviour tells a
probe apart by ``sparql == PROBE_UPDATE``, rejects updates that contain a marker
(``reject_containing``) and scripts failures by request number (``len(client.sent)``).
``expected_store`` gives the triples QLever should hold.

``logged`` captures the worker's JSON log lines; ``named`` picks the events of one name.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from typing import Any
from uuid import UUID

import asyncpg
import orjson
import pyoxigraph
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.bootstrap.common import QLEVER_INDEX_WATERMARK, asyncpg_dsn
from lae.db.models import Record
from lae.logging import configure_logging
from lae.rdf import decompress, parse_ntriples
from lae.sync.events import EVENT_COLUMNS, UPDATE_SEPARATOR, Event, event_from_row
from lae.sync.qlever_client import QLeverClient, QLeverError
from tests.api.conftest import (  # noqa: F401  (fixtures)
    BASE_URI,
    CONTEXT_URL,
    Network,
    id_of,
    network,
    new_uri,
    served,
)
from tests.auth.conftest import Served
from tests.publish.conftest import Caller, caller, new_draft, publish

TOKEN = "sync-test-token-5f1c"
"""Access token of the recording client (never appears in a log line or error, test 27)."""

Behaviour = Callable[["RecordingClient", str], Awaitable[None]]


class RecordingClient(QLeverClient):
    """Records every update; applies it to ``store`` unless ``behaviour`` decides otherwise."""

    def __init__(self, store: pyoxigraph.Store | None = None) -> None:
        super().__init__("http://qlever.invalid:7024", "http://qlever.invalid:7024", TOKEN)
        self.store = store if store is not None else pyoxigraph.Store()
        self.sent: list[str] = []
        self.acknowledged: list[str] = []
        self.behaviour: Behaviour | None = None

    def apply(self, sparql: str) -> None:
        """Apply ``sparql`` to the store."""
        self.store.update(sparql)

    async def update(self, sparql: str) -> None:
        self.sent.append(sparql)
        if self.behaviour is not None:
            await self.behaviour(self, sparql)
        else:
            self.apply(sparql)
        self.acknowledged.append(sparql)


def unavailable(status: int | None = 503) -> QLeverError:
    return QLeverError("QLever at http://qlever.invalid:7024 refused", status=status, body="down")


def reject_containing(marker: str, body: str = "Invalid SPARQL") -> Behaviour:
    """Reject with 400 every update that contains ``marker``; apply the others."""

    async def behaviour(c: RecordingClient, sparql: str) -> None:
        if marker in sparql:
            raise QLeverError("QLever refused", status=400, body=body)
        c.apply(sparql)

    return behaviour


# -- logs ---------------------------------------------------------------------------------------

Logged = Callable[[], list[dict[str, Any]]]


@pytest.fixture
def logged() -> Iterator[Logged]:
    """Capture the JSON log lines; call the result for the events logged so far."""
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    stream = io.StringIO()
    configure_logging("info", stream=stream)
    try:
        yield lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line]
    finally:
        root.handlers, root.level = saved


def named(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("event") == name]


def operations(update: str) -> list[str]:
    """The operations of an update (or of a batch): its parts between ``" ;\\n"``."""
    return update.split(UPDATE_SEPARATOR) if update else []


# -- stores -------------------------------------------------------------------------------------


def triples_of(store: pyoxigraph.Store) -> set[pyoxigraph.Triple]:
    return {quad.triple for quad in store}


def expected_store(lines: Iterable[str]) -> set[pyoxigraph.Triple]:
    """The triples a store holds after loading ``lines`` (values normalised like the store's)."""
    store = pyoxigraph.Store()
    text = "".join(f"{line}\n" for line in lines)
    if text:
        store.load(input=text.encode(), format=pyoxigraph.RdfFormat.N_TRIPLES)
    return triples_of(store)


def copy_store(store: pyoxigraph.Store) -> pyoxigraph.Store:
    copy = pyoxigraph.Store()
    copy.extend(list(store))
    return copy


# -- the database -------------------------------------------------------------------------------


@pytest.fixture
async def conn(db_url: str) -> AsyncIterator[asyncpg.Connection]:
    """An ``asyncpg`` connection to the test database (the worker's kind of connection)."""
    connection = await asyncpg.connect(asyncpg_dsn(db_url))
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def queue(db_url: str) -> AsyncIterator[None]:
    """An empty outbox and no watermark; every ``sync_state`` row is put back afterwards."""
    connection = await asyncpg.connect(asyncpg_dsn(db_url))
    try:
        saved = await connection.fetch("SELECT key, value::text AS value FROM sync_state")
        await connection.execute("DELETE FROM outbox_events")
        await connection.execute("DELETE FROM sync_state WHERE key = $1", QLEVER_INDEX_WATERMARK)
        yield
        async with connection.transaction():
            await connection.execute("DELETE FROM sync_state")
            await connection.executemany(
                "INSERT INTO sync_state (key, value) VALUES ($1, $2::jsonb)",
                [(row["key"], row["value"]) for row in saved],
            )
    finally:
        await connection.close()


async def all_events(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT id, record_id, kind::text AS kind, status::text AS status, attempts, last_error, "
        "processed_at, from_version, to_version FROM outbox_events ORDER BY id"
    )


async def pending_events(conn: asyncpg.Connection) -> list[Event]:
    rows = await conn.fetch(
        f"SELECT {EVENT_COLUMNS} FROM outbox_events WHERE status = 'pending' ORDER BY id"
    )
    return [event_from_row(row) for row in rows]


async def event(conn: asyncpg.Connection, event_id: int) -> Event:
    row = await conn.fetchrow(f"SELECT {EVENT_COLUMNS} FROM outbox_events WHERE id = $1", event_id)
    assert row is not None
    return event_from_row(row)


async def watermark(conn: asyncpg.Connection) -> Any:
    """The watermark as stored: ``(jsonb_typeof, text)``, or ``None`` without the key."""
    row = await conn.fetchrow(
        "SELECT jsonb_typeof(value) AS type, value::text AS value FROM sync_state WHERE key = $1",
        QLEVER_INDEX_WATERMARK,
    )
    return None if row is None else (row["type"], row["value"])


async def set_watermark(conn: asyncpg.Connection, value: Any) -> None:
    """Set the watermark to the JSON ``value``; ``...`` removes the key."""
    await conn.execute("DELETE FROM sync_state WHERE key = $1", QLEVER_INDEX_WATERMARK)
    if value is not ...:
        await conn.execute(
            "INSERT INTO sync_state (key, value) VALUES ($1, $2::jsonb)",
            QLEVER_INDEX_WATERMARK,
            json.dumps(value),
        )


async def snapshot_tables(conn: asyncpg.Connection) -> tuple[list[Any], list[Any]]:
    """Every row of ``outbox_events`` and ``sync_state``, to compare before and after."""
    events = await conn.fetch("SELECT * FROM outbox_events ORDER BY id")
    state = await conn.fetch("SELECT key, value::text FROM sync_state ORDER BY key")
    return [tuple(row) for row in events], [tuple(row) for row in state]


async def insert_event(
    conn: asyncpg.Connection,
    record_id: UUID,
    kind: str,
    from_version: int | None,
    to_version: int | None,
    *,
    drift_check: bool = False,
    status: str = "pending",
) -> int:
    """Insert an outbox event directly (for rows publish cannot produce); return its id."""
    event_id: int = await conn.fetchval(
        "INSERT INTO outbox_events (kind, record_id, from_version, to_version, payload, status) "
        "VALUES ($1::outbox_events_kind, $2, $3, $4, $5::jsonb, $6::outbox_events_status) "
        "RETURNING id",
        kind,
        record_id,
        from_version,
        to_version,
        json.dumps({"context_version": 1, "drift_check": drift_check}),
        status,
    )
    return event_id


async def revision_lines(conn: asyncpg.Connection, record_id: UUID, version: int) -> frozenset[str]:
    blob = await conn.fetchval(
        "SELECT ntriples_zstd FROM record_revisions WHERE record_id = $1 AND version = $2",
        record_id,
        version,
    )
    assert blob is not None
    return parse_ntriples(decompress(blob))


async def revision_jsonld(conn: asyncpg.Connection, record_id: UUID, version: int) -> Any:
    blob = await conn.fetchval(
        "SELECT jsonld_zstd FROM record_revisions WHERE record_id = $1 AND version = $2",
        record_id,
        version,
    )
    assert blob is not None
    return orjson.loads(decompress(blob))


# -- publishing ---------------------------------------------------------------------------------


@pytest.fixture
async def editor(db_engine: AsyncEngine) -> Caller:
    return await caller(db_engine)


def person(name: str, *more_names: str) -> dict[str, Any]:
    """Template of a ``Person`` that validates without a diagnostic (one ``Name`` per name)."""
    return {
        "_label": name,
        "identified_by": [{"type": "Name", "content": n} for n in (name, *more_names)],
    }


async def publish_ok(app: Served, who: Caller, record_id: UUID) -> int:
    """Publish the record's draft, accepting whatever warnings it has; return the version."""
    response = await publish(app, who, record_id)
    if response.status_code == 409 and response.json().get("code") == "LAE-PUB-002":
        shown = response.json()["params"]["records"]
        accepted = [
            {"code": w["code"], "path": w["path"], "record_id": rid}
            for rid, warnings in shown.items()
            for w in warnings
        ]
        response = await publish(app, who, record_id, *accepted)
    assert response.status_code == 200, response.text
    version: int = response.json()["version"]
    return version


async def publish_new(
    app: Served, who: Caller, template: dict[str, Any], record_type: str = "Person"
) -> tuple[UUID, str]:
    """Create a record from ``template`` and publish version 1; return ``(id, uri)``."""
    record_id, uri = await new_draft(app, who, record_type, template)
    assert await publish_ok(app, who, record_id) == 1
    return record_id, uri


async def current_data(engine: AsyncEngine, record_id: UUID) -> dict[str, Any]:
    """``records.data`` (with its internal node IRIs), as a fresh copy."""
    async with AsyncSession(engine) as session:
        record = await session.get(Record, record_id)
        assert record is not None
        data: dict[str, Any] = json.loads(json.dumps(record.data))
        return data


async def publish_content(
    app: Served, net: Network, who: Caller, record_id: UUID, content: dict[str, Any]
) -> int:
    """Publish ``content`` as the record's next version; return the version."""
    await net.rooms.open(record_id, content=content)
    return await publish_ok(app, who, record_id)


async def publish_edit(
    app: Served,
    net: Network,
    who: Caller,
    engine: AsyncEngine,
    record_id: UUID,
    edit: Callable[[dict[str, Any]], object],
) -> int:
    """Apply ``edit`` to a copy of ``records.data`` and publish it; return the version."""
    data = await current_data(engine, record_id)
    edit(data)
    return await publish_content(app, net, who, record_id, data)


def rename(content: str, index: int = 0) -> Callable[[dict[str, Any]], object]:
    """An edit that changes the ``content`` of the record's ``index``-th ``Name``."""

    def edit(data: dict[str, Any]) -> None:
        data["identified_by"][index]["content"] = content

    return edit


__all__ = ["BASE_URI", "CONTEXT_URL", "Network", "Served", "id_of", "new_uri"]
