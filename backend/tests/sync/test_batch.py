"""Batching, acknowledgement, watermark, failures and the process (contract §3 items 10-24).

Tests 11-27 of the contract, tests 38-53 of its §7 (issue #150: catch-up meets a ``done`` event
it cannot replay; items 25-33), and tests 56-62, 71 and 72 of its §8 (issue #171: the live path
confirms a rejection with the probe; the token is removed before a body is cut or encoded;
items 34-38, 44, 45). The recording client applies each update to a
``pyoxigraph.Store`` standing in for QLever; the tests check it holds exactly the stored triples
of the records' last versions. Crashes are injected by the client or a connection class; time
by patching ``worker.pause``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID

import asyncpg
import httpx
import pyoxigraph
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from lae.bootstrap.common import asyncpg_dsn
from lae.logging import configure_logging
from lae.rdf import compress, expand_to_ntriples
from lae.settings import Settings
from lae.sync import metrics as sync_metrics
from lae.sync import worker
from lae.sync.errors import (
    REJECTED,
    REVISION_MISSING,
    UNREADABLE,
    UNTRANSLATABLE,
    EventError,
)
from lae.sync.events import translate_event
from lae.sync.metrics import Metrics
from lae.sync.qlever_client import QLeverClient, QLeverError
from lae.sync.worker import (
    BATCH_SIZE,
    EXIT_CONFIG_ERROR,
    MAX_ATTEMPTS,
    PROBE_UPDATE,
    SYNC_WORKER_LOCK,
    ConnectionLostError,
    Refusal,
    UpdatesRefusedError,
    apply_event,
    catch_up,
    classify,
    process_batch,
    replay,
)
from tests.api.conftest import AAT, insert_record, titled_record
from tests.publish.conftest import Caller
from tests.publish.test_publish import set_bootstrap_versions
from tests.sync.conftest import (
    TOKEN,
    Logged,
    Network,
    RecordingClient,
    Served,
    all_events,
    copy_store,
    event,
    expected_store,
    id_of,
    insert_event,
    named,
    new_uri,
    operations,
    pending_events,
    person,
    publish_content,
    publish_edit,
    publish_new,
    reject_containing,
    rename,
    revision_lines,
    set_watermark,
    snapshot_tables,
    triples_of,
    unavailable,
    watermark,
)

pytestmark = pytest.mark.usefixtures("queue")

QLEVER_URL = "http://qlever.test:7024"


@dataclass
class Library:
    """Published records and what QLever should hold for them."""

    conn: asyncpg.Connection
    records: dict[UUID, str]  # id -> uri

    async def expected(self) -> set[pyoxigraph.Triple]:
        lines: set[str] = set()
        for record_id in self.records:
            version = await self.conn.fetchval(
                "SELECT version FROM records WHERE id = $1", record_id
            )
            lines |= await revision_lines(self.conn, record_id, version)
        return expected_store(lines)


@dataclass
class World:
    served: Served
    network: Network
    editor: Caller
    engine: AsyncEngine
    conn: asyncpg.Connection

    def library(self, *record_ids: UUID) -> Library:
        return Library(self.conn, dict.fromkeys(record_ids, ""))

    async def new(self, name: str, *more: str) -> UUID:
        record_id, _ = await publish_new(self.served, self.editor, person(name, *more))
        return record_id

    async def edit(self, record_id: UUID, edit: Callable[[dict[str, Any]], object]) -> None:
        await publish_edit(self.served, self.network, self.editor, self.engine, record_id, edit)


@pytest.fixture
def world(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
) -> World:
    return World(served, network, editor, db_engine, conn)


@pytest.fixture
async def other(db_url: str) -> AsyncIterator[asyncpg.Connection]:
    """A second connection, for looking at Postgres from inside the client."""
    connection = await asyncpg.connect(asyncpg_dsn(db_url))
    try:
        yield connection
    finally:
        await connection.close()


async def five_events(world: World) -> tuple[list[UUID], list[int]]:
    """3 records, 5 events: A v1, B v1, A v2, C v1, B v2. Returns record ids and event ids."""
    a = await world.new("Alpha", "Alpha second")
    b = await world.new("Beta")
    await world.edit(a, rename("Alpha renamed"))
    c = await world.new("Gamma")
    await world.edit(b, rename("Beta renamed"))
    ids = [event.id for event in await pending_events(world.conn)]
    assert len(ids) == 5
    return [a, b, c], ids


def http_client(handler: Callable[[httpx.Request], httpx.Response]) -> QLeverClient:
    return QLeverClient(
        QLEVER_URL,
        QLEVER_URL,
        TOKEN,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def worker_settings(db_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": db_url,
        "qlever_url": None,
        "qlever_update_url": None,
        "qlever_access_token": None,
        "sync_metrics_port": None,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Patch the loop's sleep: record each duration and return at once (stop still counts)."""
    durations: list[float] = []

    async def pause(seconds: float, stop: asyncio.Event) -> None:
        durations.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(worker, "pause", pause)
    return durations


# -- 11-13: one update, in id order, recorded after the acknowledgement -------------------------


async def test_a_batch_is_one_update_in_id_order_and_moves_the_watermark(world: World) -> None:
    records, ids = await five_events(world)
    client = RecordingClient()
    expected_updates = [
        await translate_event(world.conn, e) for e in await pending_events(world.conn)
    ]

    result = await process_batch(world.conn, client)

    assert (result.outcome, result.done, result.updates_sent) == ("done", tuple(ids), 1)
    assert client.sent == [" ;\n".join(expected_updates)]
    rows = await all_events(world.conn)
    assert [row["status"] for row in rows] == ["done"] * 5
    assert all(row["processed_at"] is not None for row in rows)
    assert await watermark(world.conn) == ("number", str(max(ids)))
    assert triples_of(client.store) == await world.library(*records).expected()


async def test_at_most_fifty_events_per_batch_lowest_ids_first(world: World) -> None:
    records = [await world.new(f"Person {n}") for n in range(60)]
    ids = [event.id for event in await pending_events(world.conn)]
    client = RecordingClient()

    first = await process_batch(world.conn, client)
    second = await process_batch(world.conn, client)
    third = await process_batch(world.conn, client)

    assert first.done == tuple(ids[:BATCH_SIZE]) and second.done == tuple(ids[BATCH_SIZE:])
    assert third.outcome == "empty" and len(client.sent) == 2
    assert await watermark(world.conn) == ("number", str(ids[-1]))
    assert triples_of(client.store) == await world.library(*records).expected()


async def test_nothing_is_recorded_before_qlever_acknowledges(
    world: World, other: asyncpg.Connection
) -> None:
    records, first_ids = await five_events(world)
    client = RecordingClient()
    assert (await process_batch(world.conn, client)).outcome == "done"
    before = await watermark(world.conn)
    await world.edit(records[0], rename("Alpha again"))
    seen: list[tuple[list[str], Any]] = []

    async def inspect(c: RecordingClient, sparql: str) -> None:
        rows = await other.fetch(
            "SELECT status::text FROM outbox_events WHERE id > $1", max(first_ids)
        )
        state = await other.fetchrow(
            "SELECT jsonb_typeof(value) AS type, value::text AS value FROM sync_state "
            "WHERE key = 'qlever_index_watermark'"
        )
        seen.append(([row[0] for row in rows], (state["type"], state["value"])))
        c.apply(sparql)

    client.behaviour = inspect
    result = await process_batch(world.conn, client)

    assert result.outcome == "done"
    assert seen == [(["pending"], before)]
    assert await watermark(world.conn) == ("number", str(result.done[-1]))


# -- 14-16: unavailable, lost answers, partial updates ------------------------------------------


@pytest.mark.parametrize("failure", ["connect", "timeout", 500, 503, 401, 429])
async def test_an_unacknowledged_batch_stays_pending_and_costs_no_attempt(
    world: World, failure: str | int
) -> None:
    await five_events(world)
    await set_watermark(world.conn, 3)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "connect":
            raise httpx.ConnectError("connection refused", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        assert isinstance(failure, int)
        return httpx.Response(failure, text="not now")

    before = await snapshot_tables(world.conn)
    async with http_client(handler) as client:
        result = await process_batch(world.conn, client)
        assert (result.outcome, result.done, result.failed_event) == ("unavailable", (), None)
        assert result.updates_sent == 0 and result.error
        assert await snapshot_tables(world.conn) == before
        for _ in range(20):
            assert (await process_batch(world.conn, client)).outcome == "unavailable"
    assert await snapshot_tables(world.conn) == before
    assert {row["status"] for row in await all_events(world.conn)} == {"pending"}
    assert {row["attempts"] for row in await all_events(world.conn)} == {0}
    assert len(requests) == 21
    assert requests[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_classify_tells_unavailable_from_rejected() -> None:
    def error(status: int | None) -> QLeverError:
        return QLeverError("x", status=status, body="")

    for status in (None, 401, 403, 408, 429, 500, 502, 503, 504, 599):
        assert classify(error(status)) == "unavailable", status
    for status in (400, 404, 405, 409, 413, 414, 415, 422, 301):
        assert classify(error(status)) == "rejected", status
    assert [worker.backoff_seconds(n) for n in range(1, 9)] == [1, 2, 4, 8, 16, 30, 30, 30]


async def test_a_crash_after_qlever_applied_is_repaired_by_sending_again(world: World) -> None:
    records, ids = await five_events(world)
    client = RecordingClient()

    async def lost_answer(c: RecordingClient, sparql: str) -> None:
        c.apply(sparql)
        raise unavailable(None)

    client.behaviour = lost_answer
    assert (await process_batch(world.conn, client)).outcome == "unavailable"
    client.behaviour = None
    again = await process_batch(world.conn, client)

    assert again.done == tuple(ids) and len(client.sent) == 2
    assert client.sent[0] == client.sent[1]
    assert triples_of(client.store) == await world.library(*records).expected()
    assert (await process_batch(world.conn, client)).outcome == "empty"


async def test_a_crash_part_way_through_an_update_is_repaired(
    world: World, db_engine: AsyncEngine
) -> None:
    """Every prefix of the batch's operations, then the retry: the store is exact each time."""
    a = await world.new("Alpha", "Alpha second")
    b = await world.new("Beta")
    await set_bootstrap_versions(db_engine, None, None)
    uri = new_uri()
    loaded = titled_record(uri, "A title", "Un titre", f"{AAT}300033618")
    await insert_record(db_engine, loaded, version=1)
    c = id_of(uri)
    base = RecordingClient()
    base.apply(
        "INSERT DATA {\n"
        + "".join(f"{line}\n" for line in expand_to_ntriples(loaded, 1, record_uri=uri))
        + "}"
    )
    assert (await process_batch(world.conn, base)).outcome == "done"
    base_watermark = await watermark(world.conn)

    def drop_second(data: dict[str, Any]) -> None:
        data["identified_by"][0]["content"] = "Alpha two"
        del data["identified_by"][1]

    def add_back(data: dict[str, Any]) -> None:
        data["identified_by"].append({"type": "Name", "content": "Alpha third"})

    await world.edit(a, drop_second)
    await world.edit(b, rename("Beta two"))
    edited = json.loads(json.dumps(loaded))
    del edited["produced_by"]
    await publish_content(world.served, world.network, world.editor, c, edited)
    await world.edit(a, add_back)
    pending = await pending_events(world.conn)
    assert [e.drift_check for e in pending] == [False, False, True, False]
    ids = [e.id for e in pending]
    expected = await world.library(a, b, c).expected()
    batch = " ;\n".join([await translate_event(world.conn, e) for e in pending])
    ops = operations(batch)
    assert len(ops) > 8

    for k in range(len(ops)):
        await world.conn.execute(
            "UPDATE outbox_events SET status = 'pending', processed_at = NULL "
            "WHERE id = ANY($1::bigint[])",
            ids,
        )
        await set_watermark(world.conn, json.loads(base_watermark[1]))
        client = RecordingClient(copy_store(base.store))

        async def crash(c: RecordingClient, sparql: str, k: int = k) -> None:
            if k:
                c.apply(" ;\n".join(operations(sparql)[:k]))
            raise unavailable(None)

        client.behaviour = crash
        assert (await process_batch(world.conn, client)).outcome == "unavailable"
        client.behaviour = None
        assert (await process_batch(world.conn, client)).done == tuple(ids), k
        assert triples_of(client.store) == expected, k


# -- 17-21: rejection, dead letters, restart ----------------------------------------------------


async def test_a_rejected_batch_is_sent_event_by_event_and_only_the_bad_event_is_counted(
    world: World,
) -> None:
    await world.new("One")
    await world.new("Two")
    await world.new("Poison three")
    await world.new("Four")
    await world.new("Five")
    pending = await pending_events(world.conn)
    ids = [event.id for event in pending]
    alone = [await translate_event(world.conn, e) for e in pending]
    client = RecordingClient()
    client.behaviour = reject_containing("Poison", body="parse error near Poison")

    result = await process_batch(world.conn, client)

    assert result.outcome == "rejected"
    assert result.done == tuple(ids[:2]) and result.failed_event == ids[2]
    assert not result.dead_lettered and result.updates_sent == 2
    # The batch, events 1, 2 and 3 alone, the probe, event 3 alone (contract §8.9).
    assert client.sent == [" ;\n".join(alone), *alone[:3], PROBE_UPDATE, alone[2]]
    rows = {row["id"]: row for row in await all_events(world.conn)}
    assert [rows[i]["status"] for i in ids] == ["done", "done", "pending", "pending", "pending"]
    assert [rows[i]["attempts"] for i in ids] == [0, 0, 1, 0, 0]
    error = rows[ids[2]]["last_error"]
    assert error.startswith(f"{REJECTED} ") and "parse error near Poison" in error
    params = json.loads(error.split(" ", 1)[1])
    assert params["event"] == ids[2] and params["status"] == 400
    assert rows[ids[3]]["last_error"] is None and rows[ids[2]]["processed_at"] is None
    assert await watermark(world.conn) == ("number", str(ids[1]))


async def test_an_event_rejected_five_times_is_a_dead_letter_and_the_queue_moves_on(
    world: World, db_url: str, slept: list[float]
) -> None:
    await world.new("Poison")
    await world.new("Later")
    poison, later = [event.id for event in await pending_events(world.conn)]
    stop = asyncio.Event()
    client = RecordingClient()

    async def behaviour(c: RecordingClient, sparql: str) -> None:
        if "Poison" in sparql:
            raise QLeverError("QLever refused", status=400, body="no")
        c.apply(sparql)
        if "Later" in sparql:
            stop.set()

    client.behaviour = behaviour
    code = await asyncio.wait_for(
        worker.run(worker_settings(db_url), client=client, stop=stop), timeout=30
    )

    assert code == 0
    rows = {row["id"]: row for row in await all_events(world.conn)}
    assert rows[poison]["status"] == "failed" and rows[poison]["attempts"] == MAX_ATTEMPTS
    assert rows[poison]["processed_at"] is not None
    assert rows[poison]["last_error"].startswith(f"{REJECTED} ")
    assert rows[later]["status"] == "done"
    assert [s for s in slept if s >= 1] == [1, 2, 4, 8]
    assert await watermark(world.conn) == ("number", str(later))
    # Each attempt: the batch, the event alone, the probe, the event again (contract §8.9).
    assert sum("Poison" in sparql for sparql in client.sent) == 3 * MAX_ATTEMPTS
    assert client.sent.count(PROBE_UPDATE) == MAX_ATTEMPTS


async def test_an_untranslatable_event_cuts_the_batch_and_is_counted_alone(world: World) -> None:
    first = await world.new("First")
    bad = await insert_event(world.conn, first, "merge", 1, 2)
    await world.new("Third")
    good, _, third = [event.id for event in await pending_events(world.conn)]
    client = RecordingClient()

    cut = await process_batch(world.conn, client)
    counted = await process_batch(world.conn, client)

    assert cut.done == (good,) and len(client.sent) == 1
    assert (counted.outcome, counted.failed_event, counted.done) == ("rejected", bad, ())
    assert counted.updates_sent == 0 and len(client.sent) == 1
    rows = {row["id"]: row for row in await all_events(world.conn)}
    assert rows[bad]["attempts"] == 1 and rows[bad]["last_error"].startswith(UNTRANSLATABLE)
    assert (rows[third]["status"], rows[third]["attempts"]) == ("pending", 0)
    assert await watermark(world.conn) == ("number", str(good))
    for _ in range(MAX_ATTEMPTS - 1):
        await process_batch(world.conn, client)
    assert (await process_batch(world.conn, client)).done == (third,)
    assert PROBE_UPDATE not in client.sent  # QLever is not asked about a translation error


async def test_a_restart_resumes_from_pending(world: World, db_url: str) -> None:
    records, ids = await five_events(world)
    first = await asyncpg.connect(asyncpg_dsn(db_url))
    try:
        down = RecordingClient()
        down.behaviour = lambda c, sparql: _raise(unavailable(503))
        assert (await process_batch(first, down)).outcome == "unavailable"

        async def killed(c: RecordingClient, sparql: str) -> None:
            raise RuntimeError("the process was killed")

        down.behaviour = killed
        with pytest.raises(RuntimeError):
            await process_batch(first, down)
    finally:
        await first.close()
    client = RecordingClient()

    result = await process_batch(world.conn, client)

    assert result.done == tuple(ids)
    assert triples_of(client.store) == await world.library(*records).expected()


async def _raise(error: Exception) -> None:
    raise error


async def test_a_done_event_is_never_sent_again(world: World) -> None:
    await five_events(world)
    client = RecordingClient()
    assert (await process_batch(world.conn, client)).outcome == "done"

    again = await process_batch(world.conn, client)

    assert (again.outcome, again.done, again.updates_sent) == ("empty", (), 0)
    assert len(client.sent) == 1


# -- 22-24: the watermark, catch-up, replay -----------------------------------------------------


async def test_a_watermark_changed_during_the_batch_is_not_overwritten(
    world: World, other: asyncpg.Connection
) -> None:
    a = await world.new("Alpha")
    client = RecordingClient()
    first = await process_batch(world.conn, client)
    await world.edit(a, rename("Alpha two"))
    await world.new("Beta")

    async def lower(c: RecordingClient, sparql: str) -> None:
        c.apply(sparql)
        await other.execute(
            "UPDATE sync_state SET value = '0'::jsonb WHERE key = 'qlever_index_watermark'"
        )

    client.behaviour = lower
    result = await process_batch(world.conn, client)

    assert first.outcome == "done"
    assert (result.outcome, result.done, result.updates_sent) == ("stale_watermark", (), 1)
    assert [row["status"] for row in await all_events(world.conn)] == ["done", "pending", "pending"]
    assert await watermark(world.conn) == ("number", "0")


async def test_catch_up_replays_done_events_above_a_lowered_watermark(
    world: World, db_url: str
) -> None:
    records, ids = await five_events(world)
    client = RecordingClient()
    states = []
    for _ in ids:
        assert len((await process_batch(world.conn, client, limit=1)).done) == 1
        states.append(copy_store(client.store))
    await world.edit(records[2], rename("Gamma two"))
    k = 1  # the index was rebuilt from an export taken after the second event
    rebuilt = RecordingClient(copy_store(states[k]))
    await set_watermark(world.conn, ids[k])

    replayed = await catch_up(world.conn, rebuilt)
    assert replayed == len(ids) - k - 1
    assert await watermark(world.conn) == ("number", str(ids[-1]))
    result = await process_batch(world.conn, rebuilt)

    assert result.outcome == "done" and len(result.done) == 1
    assert triples_of(rebuilt.store) == await world.library(*records).expected()
    assert await watermark(world.conn) == ("number", str(result.done[0]))

    # The process catches up when it starts, with --once and without.
    for once in (True, False):
        restored = RecordingClient(copy_store(states[k]))
        await set_watermark(world.conn, ids[k])
        stop = asyncio.Event()

        async def caught_up(c: RecordingClient, sparql: str, stop: asyncio.Event = stop) -> None:
            c.apply(sparql)
            stop.set()

        restored.behaviour = caught_up
        run = worker.run(worker_settings(db_url), once=once, client=restored, stop=stop)
        assert await asyncio.wait_for(run, timeout=20) == 0
        assert len(restored.sent) == 1, once
        assert triples_of(restored.store) == await world.library(*records).expected(), once
        assert await watermark(world.conn) == ("number", str(result.done[0])), once

    await set_watermark(world.conn, ...)
    untouched = RecordingClient()
    assert await catch_up(world.conn, untouched) == 0 and untouched.sent == []
    await set_watermark(world.conn, "not a number")
    with pytest.raises(worker.WatermarkError):
        await catch_up(world.conn, untouched)


async def test_a_watermark_lowered_between_batches_is_caught_up_before_any_pending_event(
    world: World, db_url: str
) -> None:
    """Review of PR #148: the index is rebuilt while no batch is in flight (items 14 and 15).

    The batch would otherwise find the lowered value both times it reads the watermark, write
    its own highest id over it, and the ``done`` events in between would never reach the index.
    """
    records, ids = await five_events(world)
    client = RecordingClient()
    states = []
    for _ in ids:
        assert len((await process_batch(world.conn, client, limit=1)).done) == 1
        states.append(copy_store(client.store))
    await world.edit(records[2], rename("Gamma two"))
    (pending,) = await pending_events(world.conn)
    k = 1
    rebuilt = RecordingClient(copy_store(states[k]))
    await set_watermark(world.conn, ids[k])
    before = await snapshot_tables(world.conn)

    result = await process_batch(world.conn, rebuilt)

    assert (result.outcome, result.done, result.updates_sent) == ("stale_watermark", (), 0)
    assert rebuilt.sent == [] and await snapshot_tables(world.conn) == before

    # The loop catches up after ``stale_watermark``, then applies the pending event.
    stop = asyncio.Event()
    started = worker.catch_up
    calls = 0

    async def catch_up_once_running(
        conn: asyncpg.Connection, c: QLeverClient, *, metrics: Any = None
    ) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 0  # nothing caught up at the start
        return await started(conn, c, metrics=metrics)

    async def until_applied(c: RecordingClient, sparql: str) -> None:
        c.apply(sparql)
        if "Gamma two" in sparql:
            stop.set()

    rebuilt.behaviour = until_applied
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(worker, "catch_up", catch_up_once_running)
        run = worker.run(worker_settings(db_url), client=rebuilt, stop=stop)
        assert await asyncio.wait_for(run, timeout=20) == 0
    assert calls == 2 and len(rebuilt.sent) == 2  # the catch-up, then the pending event
    assert triples_of(rebuilt.store) == await world.library(*records).expected()
    assert await watermark(world.conn) == ("number", str(pending.id))


async def test_the_idle_loop_catches_up_when_the_watermark_is_lowered(
    world: World, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of PR #148: with an empty queue the rebuilt index is filled at the next poll."""
    records, ids = await five_events(world)
    client = RecordingClient()
    states = []
    for _ in ids:
        assert len((await process_batch(world.conn, client, limit=1)).done) == 1
        states.append(copy_store(client.store))
    k = 2
    stop = asyncio.Event()
    waits = 0
    wait_for_work = worker._wait_for_work

    async def rebuild_while_idle(wake: asyncio.Event, stopping: asyncio.Event, s: float) -> None:
        nonlocal waits
        waits += 1
        if waits == 1:  # the loop is past its start and found nothing pending
            client.store = copy_store(states[k])
            await set_watermark(world.conn, ids[k])
            return
        await wait_for_work(wake, stopping, s)

    async def until_caught_up(c: RecordingClient, sparql: str) -> None:
        c.apply(sparql)
        stop.set()

    monkeypatch.setattr(worker, "_wait_for_work", rebuild_while_idle)
    monkeypatch.setattr(worker, "POLL_INTERVAL_S", 0.05)
    client.sent.clear()
    client.behaviour = until_caught_up

    run = worker.run(worker_settings(db_url), client=client, stop=stop)
    assert await asyncio.wait_for(run, timeout=20) == 0

    assert len(client.sent) == 1
    assert triples_of(client.store) == await world.library(*records).expected()
    assert await watermark(world.conn) == ("number", str(ids[-1]))


async def test_the_watermark_never_decreases(world: World, other: asyncpg.Connection) -> None:
    """Review of PR #148 (item 13): an event that commits late has a lower id than the watermark."""
    first = await world.new("Early")
    client = RecordingClient()
    assert (await process_batch(world.conn, client)).outcome == "done"
    late = other.transaction()
    await late.start()
    late_id = await insert_event(other, first, "publish", 1, 1)  # not committed yet
    await world.new("Later")
    (later_id,) = [event.id for event in await pending_events(world.conn)]
    assert late_id < later_id
    assert (await process_batch(world.conn, client)).done == (later_id,)
    assert await watermark(world.conn) == ("number", str(later_id))
    await late.commit()

    result = await process_batch(world.conn, client)

    assert (result.outcome, result.done) == ("done", (late_id,))
    assert await watermark(world.conn) == ("number", str(later_id))


@pytest.mark.parametrize("late_is", ["pending", "done"])
async def test_catch_up_tolerates_an_event_that_became_visible_after_a_higher_id_was_done(
    world: World, other: asyncpg.Connection, late_is: str
) -> None:
    """Issue #178, Slice C: ids are assigned before commit, so ``late`` (A v1 to v2) commits
    after ``later`` (B v1) is ``done`` and the watermark is past it. The index is then restored
    to its state at A v1 with that event's id as the watermark, as an export taken there gives
    it (``lae.bootstrap.export`` never puts the watermark above an event it lacks; §10 item 49).
    Whether ``late`` is still pending or already ``done`` below the old watermark, both events
    reach the index, each record's in id order, and the watermark ends at the highest id."""
    a = await world.new("Alpha")
    client = RecordingClient()
    first = await process_batch(world.conn, client)
    (k,) = first.done
    at_k = copy_store(client.store)
    await world.edit(a, rename("Alpha renamed"))  # revision 2; its event is re-inserted late
    await world.conn.execute("DELETE FROM outbox_events WHERE id > $1", k)
    late = other.transaction()
    await late.start()
    late_id = await insert_event(other, a, "publish", 1, 2)  # visible only to `other`
    b = await world.new("Beta")
    (later_id,) = [event.id for event in await pending_events(world.conn)]
    assert k < late_id < later_id
    assert (await process_batch(world.conn, client)).done == (later_id,)
    await late.commit()
    if late_is == "done":
        assert (await process_batch(world.conn, client)).done == (late_id,)
        assert await watermark(world.conn) == ("number", str(later_id))

    restored = RecordingClient(copy_store(at_k))
    await set_watermark(world.conn, k)
    replayed = await catch_up(world.conn, restored)
    result = await process_batch(world.conn, restored)

    if late_is == "done":
        assert (replayed, result.outcome) == (2, "empty")
        assert len(restored.sent) == 1  # one batch, both events, in id order
    else:
        assert (replayed, result.done) == (1, (late_id,))
    assert triples_of(restored.store) == await world.library(a, b).expected()
    assert await watermark(world.conn) == ("number", str(later_id))
    assert [row["status"] for row in await all_events(world.conn)] == ["done"] * 3


async def test_a_notification_during_the_pending_count_still_wakes_the_loop(
    world: World, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of PR #148 (item 23): the wake flag is cleared before the count, not after it.

    A publish commits while the idle loop is reading the number of pending events (which is
    still 0 in its answer). The poll interval is a minute here, so only the notification can
    wake the loop in time.
    """
    refresh = Metrics.refresh
    calls = 0

    async def publish_during_the_count(self: Metrics, conn: asyncpg.Connection) -> None:
        nonlocal calls
        calls += 1
        await refresh(self, conn)
        if calls == 1:
            assert self.pending == 0
            delivered = asyncio.Event()
            await conn.add_listener(worker.OUTBOX_CHANNEL, lambda *_: delivered.set())
            await world.new("Noticed")
            await asyncio.wait_for(delivered.wait(), timeout=10)
            await asyncio.sleep(0)  # the worker's own listener has run as well

    monkeypatch.setattr(Metrics, "refresh", publish_during_the_count)
    monkeypatch.setattr(worker, "POLL_INTERVAL_S", 60.0)
    stop = asyncio.Event()
    client = RecordingClient()

    async def until_applied(c: RecordingClient, sparql: str) -> None:
        c.apply(sparql)
        stop.set()

    client.behaviour = until_applied
    run = worker.run(worker_settings(db_url), client=client, stop=stop)
    assert await asyncio.wait_for(run, timeout=15) == 0
    assert [row["status"] for row in await all_events(world.conn)] == ["done"]


async def test_replay_and_apply_event_write_nothing_to_postgres(world: World) -> None:
    records, ids = await five_events(world)
    live = RecordingClient()
    assert (await process_batch(world.conn, live, limit=2)).outcome == "done"
    assert (await process_batch(world.conn, live)).outcome == "done"
    before = await snapshot_tables(world.conn)
    second = RecordingClient()

    last = await replay(world.conn, second, after_id=0, batch_size=2)
    sent = await apply_event(world.conn, ids[-1], second)

    assert last == ids[-1] and sent and second.sent[-1] == sent
    assert len(second.sent) == 4  # three batches of at most two, then the one event
    assert await snapshot_tables(world.conn) == before
    assert triples_of(second.store) == triples_of(live.store)
    assert triples_of(second.store) == await world.library(*records).expected()
    assert await replay(world.conn, second, after_id=ids[-1]) == ids[-1]
    assert await replay(world.conn, second, after_id=0, through_id=ids[1]) == ids[1]
    with pytest.raises(LookupError):
        await apply_event(world.conn, ids[-1] + 1000, second)


# -- 25-27: the process -------------------------------------------------------------------------


async def test_a_second_worker_does_not_process_while_the_first_holds_the_lock(
    world: World, db_url: str, other: asyncpg.Connection
) -> None:
    await five_events(world)
    client = RecordingClient()
    assert await other.fetchval("SELECT pg_try_advisory_lock($1)", SYNC_WORKER_LOCK)

    held = await worker.run(worker_settings(db_url), once=True, client=client)

    assert held == 1 and client.sent == []
    assert {row["status"] for row in await all_events(world.conn)} == {"pending"}
    await other.execute("SELECT pg_advisory_unlock($1)", SYNC_WORKER_LOCK)
    assert await worker.run(worker_settings(db_url), once=True, client=client) == 0
    assert {row["status"] for row in await all_events(world.conn)} == {"done"}


async def test_an_oversized_batch_is_cut_at_an_event_boundary(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    records, ids = await five_events(world)
    updates = [await translate_event(world.conn, e) for e in await pending_events(world.conn)]
    sizes = [len(u.encode()) for u in updates]
    monkeypatch.setattr(worker, "MAX_UPDATE_BYTES", sizes[0] + 3 + sizes[1])
    client = RecordingClient()

    first = await process_batch(world.conn, client)
    assert first.done == tuple(ids[:2]) and client.sent == [" ;\n".join(updates[:2])]

    monkeypatch.setattr(worker, "MAX_UPDATE_BYTES", 10)  # smaller than any one event
    while (await process_batch(world.conn, client)).outcome == "done":
        pass
    assert client.sent[1:] == updates[2:]
    assert triples_of(client.store) == await world.library(*records).expected()


async def test_once_processes_one_batch_and_exits(world: World, db_url: str) -> None:
    for n in range(BATCH_SIZE + 2):
        await world.new(f"Once {n}")
    client = RecordingClient()
    settings = worker_settings(db_url)

    assert await worker.run(settings, once=True, client=client) == 0
    assert len(client.sent) == 1
    statuses = [row["status"] for row in await all_events(world.conn)]
    assert statuses.count("done") == BATCH_SIZE and statuses.count("pending") == 2
    assert await worker.run(settings, once=True, client=client) == 0
    assert await worker.run(settings, once=True, client=client) == 0  # empty
    down = RecordingClient()
    down.behaviour = lambda c, sparql: _raise(unavailable(503))
    await world.new("Once more")
    assert await worker.run(settings, once=True, client=down) == 1


async def test_the_worker_refuses_to_start_without_url_or_token(
    world: World, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from lae.cli import EXIT_CONFIG_ERROR as CLI_CONFIG_ERROR

    assert EXIT_CONFIG_ERROR == CLI_CONFIG_ERROR
    no_url = worker_settings(db_url, qlever_access_token=SecretStr("t"))
    no_token = worker_settings(db_url, qlever_url=QLEVER_URL)
    assert await worker.run(no_url, once=True) == EXIT_CONFIG_ERROR
    assert "LAE_QLEVER_URL" in capsys.readouterr().err
    assert await worker.main(no_token, ["--once"]) == EXIT_CONFIG_ERROR
    assert "LAE_QLEVER_ACCESS_TOKEN" in capsys.readouterr().err
    assert await worker.main(no_token, ["--replay-after", "0"]) == EXIT_CONFIG_ERROR

    await world.new("Watermark")
    await set_watermark(world.conn, "seven")
    client = RecordingClient()
    assert await worker.run(worker_settings(db_url), once=True, client=client) == EXIT_CONFIG_ERROR
    assert client.sent == []
    with pytest.raises(SystemExit):
        worker.parse_arguments(["--through", "5"])


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


async def scrape(port: int, path: str = "/metrics") -> httpx.Response:
    async with httpx.AsyncClient() as http:
        return await http.get(f"http://127.0.0.1:{port}{path}", timeout=5)


def parse_metrics(response: httpx.Response) -> dict[str, float]:
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    values: dict[str, float] = {}
    for line in response.text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.split(" ")
            values[name] = float(value)
    return values


async def metrics_when(
    port: int, name: str, value: float, *, at_most: bool = False, seconds: float = 10.0
) -> dict[str, float]:
    """Scrape until ``name`` is at least (or at most) ``value``; return the metrics."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        try:
            values = parse_metrics(await scrape(port))
        except httpx.HTTPError:
            values = {}
        found = values.get(name)
        if found is not None and (found <= value if at_most else found >= value):
            return values
        assert asyncio.get_running_loop().time() < deadline, (name, values)
        await asyncio.sleep(0.05)


async def sent_when(client: RecordingClient, count: int, *, seconds: float = 10.0) -> None:
    """Wait until ``client`` was sent at least ``count`` updates (acknowledged or not)."""
    deadline = asyncio.get_running_loop().time() + seconds
    while len(client.sent) < count:
        assert asyncio.get_running_loop().time() < deadline, client.sent
        await asyncio.sleep(0.01)


async def test_metrics_report_pending_failed_lag_and_updates_sent(
    world: World, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def pause(seconds: float, stop: asyncio.Event) -> None:
        await asyncio.sleep(0.02)

    monkeypatch.setattr(worker, "pause", pause)
    first = await world.new("Metrics one")
    await world.new("Metrics two")
    await insert_event(world.conn, first, "publish", 1, 1, status="failed")
    await world.conn.execute(
        "UPDATE outbox_events SET created_at = now() - interval '90 seconds' "
        "WHERE status = 'pending'"
    )
    port = free_port()
    stop = asyncio.Event()
    client = RecordingClient()
    client.behaviour = lambda c, sparql: _raise(unavailable(503))
    task = asyncio.create_task(
        worker.run(worker_settings(db_url, sync_metrics_port=port), client=client, stop=stop)
    )
    try:
        down = await metrics_when(port, "sync_unavailable_total", 2)
        assert down["outbox_pending"] == 2 and down["outbox_failed"] == 1
        assert down["sync_lag_seconds"] >= 90 and down["updates_sent_total"] == 0
        assert down["sync_watermark"] != down["sync_watermark"]  # NaN: no watermark yet

        client.behaviour = None
        await metrics_when(port, "updates_sent_total", 1)
        up = await metrics_when(port, "outbox_pending", 0, at_most=True)
        assert up["updates_sent_total"] == 1 and up["sync_events_done_total"] == 2
        assert up["sync_lag_seconds"] == 0 and up["outbox_failed"] == 1
        assert up["sync_dead_letters_total"] == 0
        assert up["sync_watermark"] == max(
            e["id"] for e in await all_events(world.conn) if e["status"] == "done"
        )
        assert (await scrape(port, "/other")).status_code == 404
    finally:
        stop.set()
        assert await asyncio.wait_for(task, timeout=10) == 0


async def test_the_token_is_in_no_log_line_or_error(
    world: World,
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """QLever's answers echo the token here; the worker must not repeat it anywhere.

    The worker builds its client from the settings (``LAE_QLEVER_URL``,
    ``LAE_QLEVER_ACCESS_TOKEN``); only the HTTP transport is replaced. The first run meets a
    live refusal (the probe rejected) and then a confirmed rejection (contract §8, test 72). A
    second run catches up after the watermark is lowered (contract §7, test 53): the probe is
    refused once, then the first event becomes a catch-up dead letter. Last, ``--replay-after``
    stops on a rejection and says so on stderr.
    """

    async def pause(seconds: float, stop: asyncio.Event) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(worker, "pause", pause)
    await world.new("Token one")
    await world.new("Token two")
    # Unavailable twice; the batch, its first event alone, the probe refused (a live refusal);
    # the batch, its first event alone, the probe accepted, the first event rejected again.
    answers = [503, 401, 400, 400, 404, 400, 400, 200, 400]
    stop = asyncio.Event()
    seen: list[str] = []
    catching_up = replaying = False

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        echo = f"bad request with {request.headers['authorization']} and {TOKEN}"
        if replaying:
            return httpx.Response(400, text=echo)
        if catching_up:
            update = parse_qs(request.content.decode())["update"][0]
            if answers:  # the batch, its first event alone, the probe: refused
                return httpx.Response(answers.pop(0), text=echo)
            if "Token one" in update:
                return httpx.Response(400, text=echo)
            if "Token two" in update:
                stop.set()
            return httpx.Response(200, json={"status": "OK"})
        status = answers.pop(0) if answers else 200
        if status == 200:
            if not answers:
                stop.set()
            return httpx.Response(200, json={"status": "OK"})
        return httpx.Response(status, text=echo)

    real_client = httpx.AsyncClient

    def with_transport(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", with_transport)
    settings = worker_settings(db_url, qlever_url=QLEVER_URL, qlever_access_token=SecretStr(TOKEN))
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    stream = io.StringIO()
    configure_logging("debug", stream=stream)
    try:
        code = await asyncio.wait_for(worker.run(settings, stop=stop), timeout=30)
        first, second = [row["id"] for row in await all_events(world.conn)]
        await set_watermark(world.conn, first - 1)
        catching_up, answers, stop = True, [404, 404, 404], asyncio.Event()
        caught_up = await asyncio.wait_for(worker.run(settings, stop=stop), timeout=30)
        replaying = True
        capsys.readouterr()
        replayed = await worker.main(settings, ["--replay-after", str(first - 1)])
    finally:
        root.handlers, root.level = saved
    assert code == caught_up == 0 and replayed == 1
    stderr = capsys.readouterr().err
    assert "replay stopped" in stderr and "[redacted]" in stderr and TOKEN not in stderr
    assert set(seen) == {f"Bearer {TOKEN}"}
    logged = stream.getvalue()
    assert "sync_unavailable" in logged and "sync_event_failed" in logged
    assert "sync_updates_refused" in logged and "sync_dead_letter" in logged
    lines = [json.loads(line) for line in logged.splitlines() if line]
    refused = named(lines, "sync_updates_refused")
    assert [r["during"] for r in refused] == ["live", "catch_up"]
    assert all("[redacted]" in r["body"] for r in refused)
    rows = await all_events(world.conn)
    assert [(row["id"], row["status"]) for row in rows] == [(first, "failed"), (second, "done")]
    assert '"catch_up": true' in rows[0]["last_error"]
    errors = [row["last_error"] for row in rows if row["last_error"]]
    assert errors and all("[redacted]" in error for error in errors)
    assert TOKEN not in logged and all(TOKEN not in error for error in errors)
    assert TOKEN not in Metrics().render()


# -- 38-53: catch-up meets a done event it cannot replay (contract §7, issue #150) --------------

MARKER = "Poison marker 5e1d"
"""A literal that version 2 of record R adds and version 3 removes; the client rejects it."""


def add_name(content: str) -> Callable[[dict[str, Any]], object]:
    def edit(data: dict[str, Any]) -> None:
        data["identified_by"].append({"type": "Name", "content": content})

    return edit


def drop_name(content: str) -> Callable[[dict[str, Any]], object]:
    def edit(data: dict[str, Any]) -> None:
        data["identified_by"] = [n for n in data["identified_by"] if n.get("content") != content]

    return edit


@dataclass
class Poisoned:
    """The poisoned world of contract §7.7, every event ``done``, the watermark lowered to ``k``.

    Events, in id order: ``k`` = A v1, B v1, R v1, A v2, ``E`` = R v1→v2 (adds the marker), and
    with a tail: B v2, ``E2`` = R v2→v3 (removes it), A v3. ``states[i]`` is the store after
    ``ids[i]``. The marker is in ``E``'s update and in ``E2``'s as a ``DIFF``, not as a
    ``REPLACE``, and never in the probe.
    """

    world: World
    records: tuple[UUID, ...]
    r: UUID
    ids: list[int]
    states: list[pyoxigraph.Store]
    e: int
    e2: int | None

    @property
    def k(self) -> int:
        return self.ids[0]

    @property
    def above_k(self) -> list[int]:
        return self.ids[1:]

    @property
    def before_e(self) -> list[int]:
        """The events between ``k`` and ``E``: in ``E``'s batch, before it."""
        return [i for i in self.ids if self.k < i < self.e]

    def client(self, at: int = 0) -> RecordingClient:
        """A client whose store is the state at ``ids[at]``; it rejects the marker with 400."""
        client = RecordingClient(copy_store(self.states[at]))
        client.behaviour = reject_containing(MARKER)
        return client

    async def expected(self, *more: UUID) -> set[pyoxigraph.Triple]:
        return await self.world.library(*self.records, *more).expected()

    async def rows(self) -> dict[int, asyncpg.Record]:
        return {row["id"]: row for row in await all_events(self.world.conn)}


async def process_live(world: World) -> tuple[list[int], list[pyoxigraph.Store]]:
    """Process the pending events one by one; return their ids and the store after each."""
    ids = [e.id for e in await pending_events(world.conn)]
    live = RecordingClient()  # accepts everything: the marker was accepted when published
    states = []
    for _ in ids:
        assert len((await process_batch(world.conn, live, limit=1)).done) == 1
        states.append(copy_store(live.store))
    return ids, states


async def poisoned_world(world: World, *, tail: bool = True) -> Poisoned:
    a = await world.new("Alpha")
    b = await world.new("Beta")
    r = await world.new("Rembrandt")
    await world.edit(a, rename("Alpha two"))
    await world.edit(r, add_name(MARKER))
    if tail:
        await world.edit(b, rename("Beta two"))
        await world.edit(r, drop_name(MARKER))
        await world.edit(a, rename("Alpha three"))
    ids, states = await process_live(world)
    poisoned = Poisoned(world, (a, b, r), r, ids, states, ids[4], ids[6] if tail else None)
    await set_watermark(world.conn, poisoned.k)
    return poisoned


async def assert_ends_as_in_38(
    p: Poisoned, client: RecordingClient, before: dict[int, asyncpg.Record]
) -> None:
    """Test 38's end state: ``E`` a catch-up dead letter, the rest replayed, the store exact."""
    conn = p.world.conn
    rows = await p.rows()
    dead = rows[p.e]
    assert dead["status"] == "failed"
    error = dead["last_error"]
    assert error.startswith(f"{REJECTED} ")
    params = json.loads(error.split(" ", 1)[1])
    assert (params["event"], params["status"], params["catch_up"]) == (p.e, 400, True)
    assert dead["processed_at"] is not None
    assert dead["processed_at"] != before[p.e]["processed_at"]
    assert dead["attempts"] == before[p.e]["attempts"]
    for event_id, row in rows.items():
        if event_id != p.e:
            assert tuple(row) == tuple(before[event_id]), event_id
    assert p.e2 is not None
    # E2 went as a replace: DELETE WHERE, then INSERT DATA, and no DELETE DATA.
    replace = await translate_event(conn, await event(conn, p.e2))
    assert replace.startswith("DELETE WHERE") and "DELETE DATA" not in replace
    assert replace.index("DELETE WHERE") < replace.index("INSERT DATA")
    assert any(replace in sent for sent in client.acknowledged)
    assert triples_of(client.store) == await p.expected()
    highest = max(i for i, row in rows.items() if row["status"] == "done")
    assert await watermark(conn) == ("number", str(highest))


async def test_catch_up_dead_letters_a_rejected_done_event_and_replays_the_rest(
    world: World,
) -> None:
    """Issue #150: the worker used to exit 1 here, and again at every start."""
    p = await poisoned_world(world)
    before = await p.rows()
    client = p.client()

    replayed = await catch_up(world.conn, client)

    await assert_ends_as_in_38(p, client, before)
    assert replayed == len(p.above_k) - 1


async def test_a_rejection_is_confirmed_by_a_probe_and_a_second_send(world: World) -> None:
    p = await poisoned_world(world)
    alone = [await translate_event(world.conn, await event(world.conn, i)) for i in p.before_e]
    e_update = await translate_event(world.conn, await event(world.conn, p.e))
    client = p.client()

    await catch_up(world.conn, client)

    assert MARKER in client.sent[0] and len(operations(client.sent[0])) > len(operations(e_update))
    assert client.sent[1 : 4 + len(alone)] == [*alone, e_update, PROBE_UPDATE, e_update]
    assert client.sent.count(PROBE_UPDATE) == 1
    rows = await p.rows()
    assert all(rows[i]["status"] == "done" and rows[i]["last_error"] is None for i in p.before_e)


async def test_an_event_accepted_on_the_second_send_is_not_a_dead_letter(world: World) -> None:
    p = await poisoned_world(world)
    client = p.client()
    rejections = 0

    async def flaky(c: RecordingClient, sparql: str) -> None:
        nonlocal rejections
        if MARKER in sparql and rejections < 2:  # the combined update, then E sent alone
            rejections += 1
            raise QLeverError("QLever refused", status=400, body="once")
        c.apply(sparql)

    client.behaviour = flaky
    assert p.e2 is not None
    diff = await translate_event(world.conn, await event(world.conn, p.e2))
    assert diff.startswith("DELETE DATA") and MARKER in diff

    replayed = await catch_up(world.conn, client)

    assert replayed == len(p.above_k)
    assert {row["status"] for row in (await p.rows()).values()} == {"done"}
    assert client.sent.count(PROBE_UPDATE) == 1
    assert diff in client.acknowledged
    assert triples_of(client.store) == await p.expected()
    assert await watermark(world.conn) == ("number", str(p.ids[-1]))


async def corrupt(conn: asyncpg.Connection, record_id: UUID, version: int, how: str) -> None:
    """Make a stored revision unreadable (``-002``) or remove it (``-001``)."""
    if how == "deleted revision":
        await conn.execute(
            "DELETE FROM record_revisions WHERE record_id = $1 AND version = $2",
            record_id,
            version,
        )
        return
    blob = (
        b"not zstd at all"
        if how == "corrupt zstd"
        else compress(b"<https://x.test/a> <https://x.test/b> <https://x.test/c>.\n")
    )
    await conn.execute(
        "UPDATE record_revisions SET ntriples_zstd = $3 WHERE record_id = $1 AND version = $2",
        record_id,
        version,
        blob,
    )


async def lines_at(conn: asyncpg.Connection, versions: dict[UUID, int]) -> set[pyoxigraph.Triple]:
    """The triples of the given version of each record."""
    lines: set[str] = set()
    for record_id, version in versions.items():
        lines |= await revision_lines(conn, record_id, version)
    return expected_store(lines)


@pytest.mark.parametrize("how", ["corrupt zstd", "non-canonical line", "deleted revision"])
async def test_catch_up_dead_letters_a_done_event_whose_revision_is_unreadable(
    world: World, how: str
) -> None:
    c = await world.new("Corrupt")
    a = await world.new("Alpha")  # k
    b = await world.new("Beta")
    await world.edit(c, rename("Corrupt two"))  # C's only event above k
    await world.edit(a, rename("Alpha two"))
    await world.edit(b, rename("Beta two"))
    ids, states = await process_live(world)
    bad = ids[3]
    await set_watermark(world.conn, ids[1])
    await corrupt(world.conn, c, 2, how)
    c_uri = await world.conn.fetchval("SELECT uri FROM records WHERE id = $1", c)
    client = RecordingClient(copy_store(states[1]))

    replayed = await catch_up(world.conn, client)

    rows = {row["id"]: row for row in await all_events(world.conn)}
    assert rows[bad]["status"] == "failed" and rows[bad]["attempts"] == 0
    code = REVISION_MISSING if how == "deleted revision" else UNREADABLE
    assert rows[bad]["last_error"].startswith(f"{code} ")
    assert json.loads(rows[bad]["last_error"].split(" ", 1)[1])["catch_up"] is True
    assert [i for i, row in rows.items() if row["status"] != "done"] == [bad]
    assert all(c_uri not in sent for sent in client.sent)
    assert PROBE_UPDATE not in client.sent
    assert triples_of(client.store) == await lines_at(world.conn, {a: 2, b: 2, c: 1})
    assert await watermark(world.conn) == ("number", str(ids[-1]))
    assert replayed == 3


async def test_later_events_of_a_record_with_an_unreadable_revision_are_dead_letters_too(
    world: World,
) -> None:
    c = await world.new("Corrupt")
    a = await world.new("Alpha")  # k
    await world.edit(c, rename("Corrupt two"))
    b = await world.new("Beta")
    await world.edit(c, rename("Corrupt three"))
    await world.edit(a, rename("Alpha two"))
    ids, states = await process_live(world)
    await set_watermark(world.conn, ids[1])
    await corrupt(world.conn, c, 2, "corrupt zstd")
    client = RecordingClient(copy_store(states[1]))

    await catch_up(world.conn, client)

    rows = {row["id"]: row for row in await all_events(world.conn)}
    assert [i for i, row in rows.items() if row["status"] == "failed"] == [ids[2], ids[4]]
    assert all(rows[i]["last_error"].startswith(f"{UNREADABLE} ") for i in (ids[2], ids[4]))
    assert triples_of(client.store) == await lines_at(world.conn, {a: 2, b: 1, c: 1})
    assert PROBE_UPDATE not in client.sent
    assert await watermark(world.conn) == ("number", str(ids[-1]))


async def test_an_untranslatable_done_event_inside_a_batch_is_judged_at_the_head(
    world: World, other: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await world.new("Alpha")  # k
    b = await world.new("Beta")
    (k, before_merge), states = await process_live(world)
    merge = await insert_event(world.conn, b, "merge", 1, 2, status="done")
    await set_watermark(world.conn, merge)  # as if it had been applied (a later step's kind)
    c = await world.new("Gamma")
    (last,), _ = await process_live(world)
    await set_watermark(world.conn, k)
    client = RecordingClient(copy_store(states[0]))
    judged: list[Any] = []
    dead_letter = worker._dead_letter_done

    async def record_watermark(conn: asyncpg.Connection, *args: Any) -> None:
        judged.append((len(client.sent), await watermark(other)))
        await dead_letter(conn, *args)

    monkeypatch.setattr(worker, "_dead_letter_done", record_watermark)

    replayed = await catch_up(world.conn, client)

    assert judged == [(1, ("number", str(before_merge)))]  # B v1 sent and recorded first
    assert len(client.sent) == 2 and replayed == 2
    row = next(r for r in await all_events(world.conn) if r["id"] == merge)
    assert row["status"] == "failed" and row["last_error"].startswith(f"{UNTRANSLATABLE} ")
    assert triples_of(client.store) == await world.library(a, b, c).expected()
    assert await watermark(world.conn) == ("number", str(last))


TRANSIENT: list[str | int] = ["connect", "timeout", 500, 503, 401, 403, 429]
STRIKES = ["combined", "alone", "probe", "second"]


def transient(failure: str | int) -> QLeverError:
    if failure == "connect":
        return QLeverError("QLever at x failed", body="ConnectError('connection refused')")
    if failure == "timeout":
        return QLeverError("QLever at x failed", body="ReadTimeout('timed out')")
    assert isinstance(failure, int)
    return unavailable(failure)


def strike(where: str, failure: str | int) -> Any:
    """Reject the marker; raise ``failure`` once, at the request ``where`` names."""
    struck = False

    async def behaviour(c: RecordingClient, sparql: str) -> None:
        nonlocal struck
        n = len(c.sent) - 1
        hit = {
            "combined": n == 0,
            "alone": n == 2,  # the second event sent alone after the rejected batch
            "probe": sparql == PROBE_UPDATE,
            "second": MARKER in sparql and PROBE_UPDATE in c.sent[:-1],
        }[where]
        if hit and not struck:
            struck = True
            raise transient(failure)
        if MARKER in sparql:
            raise QLeverError("QLever refused", status=400, body="Invalid SPARQL")
        c.apply(sparql)

    return behaviour


@pytest.mark.parametrize("where", STRIKES)
@pytest.mark.parametrize("failure", TRANSIENT)
async def test_a_transient_failure_during_catch_up_dead_letters_nothing(
    world: World, failure: str | int, where: str
) -> None:
    p = await poisoned_world(world)
    before = await p.rows()
    client = p.client()
    client.behaviour = strike(where, failure)

    with pytest.raises(QLeverError) as raised:
        await catch_up(world.conn, client)

    assert classify(raised.value) == "unavailable"
    assert await p.rows() == before
    acknowledged = {"combined": p.k, "alone": p.before_e[0]}.get(where, p.before_e[-1])
    assert await watermark(world.conn) == ("number", str(acknowledged))
    client.behaviour = reject_containing(MARKER)
    await catch_up(world.conn, client)
    await assert_ends_as_in_38(p, client, before)


async def test_a_long_outage_during_catch_up_dead_letters_nothing(
    world: World, other: asyncpg.Connection, slept: list[float]
) -> None:
    """Test 44, continued: 20 consecutive outages through the retrying catch-up."""
    p = await poisoned_world(world)
    before = await p.rows()
    client = p.client()
    failed_after_outages: list[int] = []
    reject = reject_containing(MARKER)

    async def down_twenty_times(c: RecordingClient, sparql: str) -> None:
        if len(c.sent) <= 20:
            raise unavailable(503)
        if len(c.sent) == 21:
            failed_after_outages.append(
                await other.fetchval("SELECT count(*) FROM outbox_events WHERE status = 'failed'")
            )
        await reject(c, sparql)

    client.behaviour = down_twenty_times
    metrics = Metrics()

    await worker._catch_up_until_done(world.conn, client, asyncio.Event(), metrics)

    assert failed_after_outages == [0]
    assert slept == [worker.backoff_seconds(n) for n in range(1, 21)]
    assert slept[:8] == [1, 2, 4, 8, 16, 30, 30, 30]
    assert metrics.sync_unavailable_total == 20
    await assert_ends_as_in_38(p, client, before)


@pytest.mark.parametrize("status", [404, 405, 400])
async def test_a_rejected_probe_blames_the_endpoint_not_the_event(
    world: World, status: int, slept: list[float], logged: Logged
) -> None:
    _, ids = await five_events(world)
    assert (await process_batch(world.conn, RecordingClient())).outcome == "done"
    await set_watermark(world.conn, ids[1])
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(parse_qs(request.content.decode())["update"][0])
        if len(requests) <= 6:  # catch_up alone, then the first round of the retrying one
            return httpx.Response(status, text=f"no updates here ({status})")
        return httpx.Response(200, json={"status": "OK"})

    before = await snapshot_tables(world.conn)
    metrics = Metrics()
    async with http_client(handler) as client:
        with pytest.raises(UpdatesRefusedError) as raised:
            await catch_up(world.conn, client)
        assert classify(raised.value) == "unavailable"
        assert raised.value.status == status and "no updates here" in raised.value.body
        assert len(requests) == 3 and requests[2] == PROBE_UPDATE
        assert await snapshot_tables(world.conn) == before

        await worker._catch_up_until_done(world.conn, client, asyncio.Event(), metrics)

    assert slept == [1]
    refused = named(logged(), "sync_updates_refused")
    assert [(e["level"], e["status"], e["consecutive"]) for e in refused] == [("error", status, 1)]
    assert "no updates here" in refused[0]["body"]
    assert metrics.sync_unavailable_total == 1
    assert {row["status"] for row in await all_events(world.conn)} == {"done"}
    assert await watermark(world.conn) == ("number", str(ids[-1]))


@pytest.mark.parametrize("batch_of_one", [False, True])
async def test_a_dead_letter_at_the_top_of_the_range_ends_catch_up(
    world: World, batch_of_one: bool
) -> None:
    p = await poisoned_world(world, tail=False)
    assert p.ids[-1] == p.e
    if batch_of_one:  # E is the only done event above the watermark
        await set_watermark(world.conn, p.before_e[-1])
        client = p.client(at=len(p.ids) - 2)
    else:
        client = p.client()
    e_update = await translate_event(world.conn, await event(world.conn, p.e))

    await catch_up(world.conn, client)

    if batch_of_one:
        assert client.sent == [e_update, PROBE_UPDATE, e_update]
    assert (await p.rows())[p.e]["status"] == "failed"
    assert await watermark(world.conn) == ("number", str(p.before_e[-1]))
    assert (await process_batch(world.conn, client)).outcome == "empty"
    await world.edit(p.r, drop_name(MARKER))
    (heal,) = await pending_events(world.conn)
    update = await translate_event(world.conn, heal)
    assert update.startswith("DELETE WHERE") and "DELETE DATA" not in update

    result = await process_batch(world.conn, client)

    assert (result.outcome, result.done) == ("done", (heal.id,))
    assert heal.id > p.e and await watermark(world.conn) == ("number", str(heal.id))
    assert triples_of(client.store) == await p.expected()


async def test_a_restart_after_a_catch_up_dead_letter_does_not_meet_the_event_again(
    world: World, db_url: str
) -> None:
    p = await poisoned_world(world)
    client = p.client()
    await catch_up(world.conn, client)
    after = await p.rows()
    sent = len(client.sent)
    restarted = await asyncpg.connect(asyncpg_dsn(db_url))
    try:
        assert await catch_up(restarted, client) == 0
    finally:
        await restarted.close()
    assert len(client.sent) == sent

    assert await worker.run(worker_settings(db_url), once=True, client=client) == 0
    assert await worker.run(worker_settings(db_url), once=True, client=client) == 0

    assert len(client.sent) == sent  # neither the event nor a probe
    rows = await p.rows()
    assert rows == after
    assert rows[p.e]["status"] == "failed"
    # The dead-letter transaction sets only a done event, exactly one row.
    current = await world.conn.fetchval(
        "SELECT value::text FROM sync_state WHERE key = 'qlever_index_watermark'"
    )
    await world.new("Pending")
    (pending,) = await pending_events(world.conn)
    for event_id in (p.e, pending.id, max(p.ids) + 1000):
        with pytest.raises(worker.RecordingError):
            await worker._dead_letter_done(world.conn, event_id, "LAE-SYNC-004 {}", current)
    rows = await p.rows()
    assert {i: row for i, row in rows.items() if i != pending.id} == after
    assert (rows[pending.id]["status"], rows[pending.id]["last_error"]) == ("pending", None)


async def test_a_crash_before_the_dead_letter_is_recorded_changes_nothing(
    world: World, db_url: str
) -> None:
    p = await poisoned_world(world)
    before = await p.rows()
    client = p.client()

    class KilledBeforeCommit(asyncpg.Connection):  # type: ignore[misc]
        async def execute(self, query: str, *args: Any, **kwargs: Any) -> str:
            status: str = await super().execute(query, *args, **kwargs)
            if "SET status = 'failed'" in query:
                raise RuntimeError("the process was killed before the commit")
            return status

    dying = await asyncpg.connect(asyncpg_dsn(db_url), connection_class=KilledBeforeCommit)
    try:
        with pytest.raises(RuntimeError, match="killed"):
            await catch_up(dying, client)
    finally:
        await dying.close()

    assert await p.rows() == before
    assert await watermark(world.conn) == ("number", str(p.before_e[-1]))
    probes = client.sent.count(PROBE_UPDATE)
    await catch_up(world.conn, client)
    assert client.sent.count(PROBE_UPDATE) == probes + 1  # judged again, dead-lettered once
    await assert_ends_as_in_38(p, client, before)


async def test_a_watermark_changed_while_an_event_is_judged_is_not_overwritten(
    world: World, other: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = await poisoned_world(world)
    before = await p.rows()
    client = p.client()
    lowered = p.before_e[0]
    lowering = True
    reject = reject_containing(MARKER)

    async def lower_during_the_second_send(c: RecordingClient, sparql: str) -> None:
        nonlocal lowering
        if lowering and MARKER in sparql and PROBE_UPDATE in c.sent[:-1]:
            lowering = False
            await set_watermark(other, lowered)
        await reject(c, sparql)

    client.behaviour = lower_during_the_second_send
    outcomes: list[Any] = []
    dead_letter = worker._dead_letter_done

    async def watched(conn: asyncpg.Connection, event_id: int, *args: Any) -> None:
        try:
            await dead_letter(conn, event_id, *args)
        except worker._StaleWatermark:
            status = await other.fetchval(
                "SELECT status::text FROM outbox_events WHERE id = $1", event_id
            )
            outcomes.append(("stale", status, await watermark(other)))
            raise
        outcomes.append(("recorded", None, None))

    monkeypatch.setattr(worker, "_dead_letter_done", watched)
    replayed_again = await translate_event(world.conn, await event(world.conn, p.before_e[1]))

    await catch_up(world.conn, client)

    assert outcomes == [("stale", "done", ("number", str(lowered))), ("recorded", None, None)]
    assert client.sent.count(PROBE_UPDATE) == 2
    assert client.sent.count(replayed_again) == 2  # sent alone again, from the lowered value
    await assert_ends_as_in_38(p, client, before)


@pytest.mark.parametrize("met_by", ["start", "idle loop"])
async def test_the_worker_keeps_running_past_a_poisoned_done_event(
    world: World,
    db_url: str,
    slept: list[float],
    logged: Logged,
    monkeypatch: pytest.MonkeyPatch,
    met_by: str,
) -> None:
    p = await poisoned_world(world)
    client = p.client()
    fetched = asyncio.Event()
    reject = reject_containing(MARKER)

    async def blocked_until_fetched(c: RecordingClient, sparql: str) -> None:
        await fetched.wait()
        await reject(c, sparql)

    if met_by == "start":
        client.behaviour = blocked_until_fetched
        await world.new("Delta")  # pending, behind the catch-up
    else:  # the index is current when the worker starts; it is restored while the loop idles
        await set_watermark(world.conn, p.ids[-1])
        client.store = copy_store(p.states[-1])
        monkeypatch.setattr(worker, "POLL_INTERVAL_S", 0.05)
    port = free_port()
    stop = asyncio.Event()
    task = asyncio.create_task(
        worker.run(worker_settings(db_url, sync_metrics_port=port), client=client, stop=stop)
    )
    try:
        if met_by == "start":
            # The listener answers from the moment it is bound, some round trips before catch-up
            # sends its first update. Scraping at once and then expecting that update made this
            # test fail about 1 run in 5 at -n 6 (issue #178): wait for the update first.
            await sent_when(client, 1)
        early = await metrics_when(port, "sync_catch_up_events_total", 0)
        assert early["sync_catch_up_events_total"] == 0
        if met_by == "start":
            assert len(client.sent) == 1  # catch-up has begun and waits for `fetched`
            assert early["outbox_pending"] == 1  # scraped while it waits (§7.5)
        else:
            client.store = copy_store(p.states[0])
            await set_watermark(world.conn, p.k)
            await metrics_when(port, "sync_catch_up_dead_letters_total", 1)
            await world.new("Delta")
        fetched.set()
        await metrics_when(port, "sync_events_done_total", 1)
        final = await metrics_when(port, "outbox_pending", 0, at_most=True)
        assert not task.done()
    finally:
        fetched.set()  # or a failure above leaves the worker in the client and hides itself
        stop.set()
        code = await asyncio.wait_for(task, timeout=10)
    assert code == 0
    assert final["outbox_failed"] == 1
    assert final["sync_dead_letters_total"] == 1
    assert final["sync_catch_up_dead_letters_total"] == 1
    assert final["sync_catch_up_events_total"] == len(p.above_k) - 1
    assert PROBE_UPDATE in client.acknowledged  # acknowledged, but not counted as sent
    assert final["updates_sent_total"] == sum(s != PROBE_UPDATE for s in client.acknowledged)
    rows = await p.rows()
    assert [row["status"] for row in rows.values()].count("failed") == 1
    delta = rows[max(rows)]
    assert delta["status"] == "done"
    dead = named(logged(), "sync_dead_letter")
    assert [(d["level"], d["during"], d["event_id"], d["record_id"]) for d in dead] == [
        ("error", "catch_up", p.e, str(p.r))
    ]
    caught_up = named(logged(), "sync_caught_up")
    assert [(c["level"], c["events"], c["dead_letters"]) for c in caught_up] == [
        ("warning", len(p.above_k) - 1, 1)
    ]
    assert triples_of(client.store) == await p.expected(delta["record_id"])


async def test_once_dead_letters_in_catch_up_without_sleeping(
    world: World, db_url: str, slept: list[float]
) -> None:
    p = await poisoned_world(world)
    before = await p.rows()
    client = p.client()

    assert await worker.run(worker_settings(db_url), once=True, client=client) == 0

    assert slept == []
    await assert_ends_as_in_38(p, client, before)


async def test_replay_still_raises_on_the_first_failure_and_writes_nothing(
    world: World,
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    logged: Logged,
) -> None:
    """``logged`` sends log lines to a stream of their own, and stdout is read only from the
    command on: the test does not depend on an earlier test having configured logging."""
    p = await poisoned_world(world)
    before = await snapshot_tables(world.conn)
    for batch_size in (BATCH_SIZE, 1):
        client = p.client()
        with pytest.raises(QLeverError) as raised:
            await replay(world.conn, client, after_id=p.k, batch_size=batch_size)
        assert raised.value.status == 400
        assert PROBE_UPDATE not in client.sent
        if batch_size == 1:
            assert len(client.sent) == len(p.before_e) + 1  # stops at E
    assert await snapshot_tables(world.conn) == before

    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        update = parse_qs(request.content.decode())["update"][0]
        sent.append(update)
        if MARKER in update:
            return httpx.Response(400, text="Invalid SPARQL")
        return httpx.Response(200, json={"status": "OK"})

    real_client = httpx.AsyncClient

    def with_transport(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", with_transport)
    settings = worker_settings(db_url, qlever_url=QLEVER_URL, qlever_access_token=SecretStr(TOKEN))
    capsys.readouterr()  # only the command's own output below

    code = await worker.main(settings, ["--replay-after", str(p.k)])

    assert code == 1
    out = capsys.readouterr()
    assert out.out.strip() == str(p.k) and "replay stopped" in out.err
    assert len(sent) == 1 and PROBE_UPDATE not in sent
    assert await snapshot_tables(world.conn) == before


async def test_failed_lists_the_dead_letters_as_json_lines(
    world: World, db_url: str, other: asyncpg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = worker_settings(db_url)  # no QLever settings at all
    assert await worker.main(settings, ["--failed"]) == 0
    assert capsys.readouterr().out == ""

    p = await poisoned_world(world)
    await catch_up(world.conn, p.client())
    assert await other.fetchval("SELECT pg_try_advisory_lock($1)", SYNC_WORKER_LOCK)
    capsys.readouterr()  # the log lines of the catch-up
    try:
        assert await worker.main(settings, ["--failed"]) == 0
    finally:
        await other.execute("SELECT pg_advisory_unlock($1)", SYNC_WORKER_LOCK)

    (line,) = capsys.readouterr().out.splitlines()
    shown = json.loads(line)
    row = (await p.rows())[p.e]
    assert list(shown) == list(worker.FAILED_FIELDS)
    assert shown["id"] == p.e and shown["kind"] == "publish"
    assert shown["record_id"] == str(p.r)
    assert (shown["from_version"], shown["to_version"], shown["attempts"]) == (1, 2, 0)
    assert shown["last_error"] == row["last_error"]
    assert shown["processed_at"] == row["processed_at"].isoformat()
    assert shown["created_at"]
    for combined in (["--failed", "--once"], ["--failed", "--replay-after", "0"]):
        with pytest.raises(SystemExit):
            worker.parse_arguments(combined)


# -- added in review (PR #170): branches the §7.7 mutation run found untested -------------------


async def test_a_lone_event_accepted_on_the_second_send_is_replayed(world: World) -> None:
    """Test 40 for a batch of one: rejected, the probe accepted, accepted the second time."""
    p = await poisoned_world(world, tail=False)
    await set_watermark(world.conn, p.before_e[-1])
    client = p.client(at=len(p.ids) - 2)
    rejections = 0

    async def flaky(c: RecordingClient, sparql: str) -> None:
        nonlocal rejections
        if MARKER in sparql and not rejections:
            rejections += 1
            raise QLeverError("QLever refused", status=400, body="once")
        c.apply(sparql)

    client.behaviour = flaky
    e_update = await translate_event(world.conn, await event(world.conn, p.e))
    metrics = Metrics()

    replayed = await catch_up(world.conn, client, metrics=metrics)

    assert replayed == 1
    assert client.sent == [e_update, PROBE_UPDATE, e_update]
    assert {row["status"] for row in (await p.rows()).values()} == {"done"}
    assert await watermark(world.conn) == ("number", str(p.e))
    assert triples_of(client.store) == await p.expected()
    assert (metrics.sync_catch_up_events_total, metrics.updates_sent_total) == (1, 1)
    assert (metrics.sync_dead_letters_total, metrics.failed) == (0, 0)


async def test_a_watermark_changed_during_a_catch_up_batch_is_not_overwritten(
    world: World, other: asyncpg.Connection
) -> None:
    """Item 28: the watermark write after an acknowledged batch is compared, as in item 14."""
    _, ids = await five_events(world)
    assert (await process_batch(world.conn, RecordingClient())).outcome == "done"
    await set_watermark(world.conn, ids[1])
    client = RecordingClient()
    seen: list[Any] = []

    async def lower_during_the_first_batch(c: RecordingClient, sparql: str) -> None:
        if len(c.sent) == 1:
            await set_watermark(other, ids[0])
        else:
            seen.append(await watermark(other))
        c.apply(sparql)

    client.behaviour = lower_during_the_first_batch

    replayed = await catch_up(world.conn, client)

    assert seen == [("number", str(ids[0]))]  # not overwritten by the batch in flight
    assert len(client.sent) == 2 and replayed == 4  # started again from the lowered value
    assert len(operations(client.sent[1])) > len(operations(client.sent[0]))
    assert await watermark(world.conn) == ("number", str(ids[-1]))


@pytest.mark.parametrize("answer", [404, 503])
async def test_once_exits_1_when_catch_up_cannot_reach_qlever_and_blames_no_event(
    world: World, db_url: str, slept: list[float], answer: int
) -> None:
    """Item 26: with ``--once`` a refused probe or an outage is exit 1, and nothing is written."""
    p = await poisoned_world(world)
    before = await snapshot_tables(world.conn)
    client = p.client()

    async def refuse_everything(c: RecordingClient, sparql: str) -> None:
        raise unavailable(answer)

    client.behaviour = refuse_everything

    assert await worker.run(worker_settings(db_url), once=True, client=client) == 1

    assert slept == []
    assert await snapshot_tables(world.conn) == before
    if answer == 404:  # the batch, its first event alone, the probe
        assert len(client.sent) == 3 and client.sent[-1] == PROBE_UPDATE


async def test_catch_up_sends_nothing_more_once_the_connection_is_closed(
    world: World, db_url: str
) -> None:
    """Item 22 in catch-up: no event is sent alone, and no probe, on a closed connection."""
    p = await poisoned_world(world)
    before = await snapshot_tables(world.conn)
    client = p.client()
    dying = await asyncpg.connect(asyncpg_dsn(db_url))
    reject = reject_containing(MARKER)

    async def close_during_the_first_request(c: RecordingClient, sparql: str) -> None:
        if not dying.is_closed():
            await dying.close()
        await reject(c, sparql)

    client.behaviour = close_during_the_first_request

    with pytest.raises(worker.ConnectionLostError):
        await catch_up(dying, client)

    assert len(client.sent) == 1
    assert await snapshot_tables(world.conn) == before


async def test_replay_raises_on_an_untranslatable_event_and_writes_nothing(world: World) -> None:
    """Test 52 for a translation error: ``replay`` neither skips it nor marks it ``failed``."""
    c = await world.new("Corrupt")
    await world.new("Alpha")
    await world.edit(c, rename("Corrupt two"))
    await world.new("Beta")
    ids, _ = await process_live(world)
    await corrupt(world.conn, c, 2, "corrupt zstd")
    before = await snapshot_tables(world.conn)
    for batch_size in (BATCH_SIZE, 1):
        client = RecordingClient()
        with pytest.raises(EventError) as raised:
            await replay(world.conn, client, after_id=ids[0], batch_size=batch_size)
        assert raised.value.code == UNREADABLE
        assert len(client.sent) == 1  # Alpha v1; nothing at or after the bad event
        assert PROBE_UPDATE not in client.sent
    assert await snapshot_tables(world.conn) == before


# -- 56-62: the live path confirms a rejection with the probe (contract §8.4, issue #171) -------


async def poisoned_third(world: World) -> tuple[list[UUID], list[int], list[str]]:
    """Five first publishes, the third poisoned: record ids, event ids, each event's update."""
    records = [await world.new(name) for name in ("One", "Two", "Poison three", "Four", "Five")]
    pending = await pending_events(world.conn)
    return records, [e.id for e in pending], [await translate_event(world.conn, e) for e in pending]


def body_of(last_error: str) -> Any:
    """The ``body`` parameter of a ``LAE-SYNC-004`` string."""
    assert last_error.startswith(f"{REJECTED} ")
    return json.loads(last_error.split(" ", 1)[1])["body"]


def rows_by_id(rows: list[asyncpg.Record]) -> dict[int, tuple[Any, ...]]:
    return {row["id"]: tuple(row) for row in rows}


@pytest.mark.parametrize("one_event", [False, True])
async def test_a_live_rejection_is_confirmed_by_a_probe_and_a_second_send(
    world: World, one_event: bool
) -> None:
    if one_event:
        await world.new("Poison alone")
        pending = await pending_events(world.conn)
        ids, alone = [pending[0].id], [await translate_event(world.conn, pending[0])]
        poison = 0
    else:
        _, ids, alone = await poisoned_third(world)
        poison = 2
    before = rows_by_id(await all_events(world.conn))
    client = RecordingClient()
    rejections = 0

    async def different_body_each_time(c: RecordingClient, sparql: str) -> None:
        nonlocal rejections
        if "Poison" in sparql:
            rejections += 1
            raise QLeverError("QLever refused", status=400, body=f"rejection {rejections}")
        c.apply(sparql)

    client.behaviour = different_body_each_time

    result = await process_batch(world.conn, client)

    if one_event:
        assert client.sent == [alone[0], PROBE_UPDATE, alone[0]]
    else:
        batch = " ;\n".join(alone)
        assert client.sent == [batch, alone[0], alone[1], alone[2], PROBE_UPDATE, alone[2]]
    assert client.sent.count(PROBE_UPDATE) == 1
    assert (result.outcome, result.failed_event, result.refusal) == ("rejected", ids[poison], None)
    assert result.done == tuple(ids[:poison]) and result.updates_sent == poison
    rows = {row["id"]: row for row in await all_events(world.conn)}
    assert [rows[i]["status"] for i in ids[:poison]] == ["done"] * poison
    bad = rows[ids[poison]]
    assert (bad["status"], bad["attempts"], bad["processed_at"]) == ("pending", 1, None)
    assert body_of(bad["last_error"]) == f"rejection {rejections}"  # the second send's answer
    assert result.error == bad["last_error"]
    for later in ids[poison + 1 :]:
        assert tuple(rows[later]) == before[later]
    expected = ("number", str(ids[poison - 1])) if poison else None
    assert await watermark(world.conn) == expected


@pytest.mark.parametrize("status", [404, 405, 400])
async def test_a_refused_probe_on_the_live_path_blames_the_endpoint_not_the_event(
    world: World,
    db_url: str,
    slept: list[float],
    logged: Logged,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """The case of issue #171: a wrong update URL answering 404 or 405 to everything."""
    records = [await world.new(name) for name in ("One", "Two", "Three")]
    pending = await pending_events(world.conn)
    alone = [await translate_event(world.conn, e) for e in pending]
    before = await snapshot_tables(world.conn)
    answer = f"no updates here ({status})"
    stop = asyncio.Event()
    client = RecordingClient()

    async def refuse_everything(c: RecordingClient, sparql: str) -> None:
        raise QLeverError("QLever refused", status=status, body=answer)

    client.behaviour = refuse_everything

    result = await process_batch(world.conn, client)

    assert result.outcome == "unavailable" and result.refusal == Refusal(status, answer)
    assert (result.failed_event, result.dead_lettered, result.done) == (None, False, ())
    assert result.updates_sent == 0 and result.error and answer in result.error
    assert client.sent == [" ;\n".join(alone), alone[0], PROBE_UPDATE]
    assert await snapshot_tables(world.conn) == before
    assert await worker.run(worker_settings(db_url), once=True, client=client) == 1
    assert await snapshot_tables(world.conn) == before

    seen: list[Metrics] = []

    class Seen(Metrics):
        def __init__(self) -> None:
            super().__init__()
            seen.append(self)

    monkeypatch.setattr(sync_metrics, "Metrics", Seen)
    client.sent.clear()
    slept.clear()
    refusals = 20

    async def refuse_twenty_rounds(c: RecordingClient, sparql: str) -> None:
        if len(c.sent) <= 3 * refusals:  # the batch, its first event, the probe: per round
            raise QLeverError("QLever refused", status=status, body=answer)
        c.apply(sparql)
        stop.set()

    client.behaviour = refuse_twenty_rounds

    code = await asyncio.wait_for(
        worker.run(worker_settings(db_url), client=client, stop=stop), timeout=30
    )

    assert code == 0
    assert client.sent.count(PROBE_UPDATE) == refusals
    backoffs = [s for s in slept if s >= 1]
    assert backoffs == [worker.backoff_seconds(n) for n in range(1, refusals + 1)]
    assert backoffs[:8] == [1, 2, 4, 8, 16, 30, 30, 30]
    events = logged()
    refused = named(events, "sync_updates_refused")
    assert [(e["level"], e["status"], e["consecutive"], e["during"]) for e in refused] == [
        ("error", status, n, "live") for n in range(1, refusals + 1)
    ]
    assert all(e["body"] == answer for e in refused)
    assert named(events, "sync_event_failed") == [] and named(events, "sync_dead_letter") == []
    assert seen[0].sync_unavailable_total == refusals
    rows = await all_events(world.conn)
    assert {(row["status"], row["attempts"], row["last_error"]) for row in rows} == {
        ("done", 0, None)
    }
    assert triples_of(client.store) == await world.library(*records).expected()


async def test_a_live_event_accepted_on_the_second_send_costs_no_attempt(world: World) -> None:
    records, ids, alone = await poisoned_third(world)
    client = RecordingClient()
    rejections = 0

    async def rejected_twice(c: RecordingClient, sparql: str) -> None:
        nonlocal rejections
        if "Poison" in sparql and rejections < 2:  # the batch, then the poison alone
            rejections += 1
            raise QLeverError("QLever refused", status=400, body="not now")
        c.apply(sparql)

    client.behaviour = rejected_twice

    result = await process_batch(world.conn, client)

    assert (result.outcome, result.done, result.updates_sent) == ("done", tuple(ids), 5)
    assert (result.failed_event, result.error, result.refusal) == (None, None, None)
    batch = " ;\n".join(alone)
    assert client.sent == [batch, *alone[:3], PROBE_UPDATE, *alone[2:]]
    rows = await all_events(world.conn)
    assert {(row["status"], row["attempts"], row["last_error"]) for row in rows} == {
        ("done", 0, None)
    }
    assert await watermark(world.conn) == ("number", str(ids[-1]))
    assert triples_of(client.store) == await world.library(*records).expected()


async def test_a_lone_live_event_accepted_on_the_second_send_is_recorded_done(
    world: World,
) -> None:
    """Test 58 for a batch of one event (added in review of #174): ``process_batch`` confirms
    that event itself, not ``_send_one_by_one``. Item 37: recorded ``done``, one update counted."""
    record = await world.new("Poison alone")
    (pending,) = await pending_events(world.conn)
    update = await translate_event(world.conn, pending)
    client = RecordingClient()
    rejections = 0

    async def rejected_once(c: RecordingClient, sparql: str) -> None:
        nonlocal rejections
        if "Poison" in sparql and rejections < 1:
            rejections += 1
            raise QLeverError("QLever refused", status=400, body="not now")
        c.apply(sparql)

    client.behaviour = rejected_once

    result = await process_batch(world.conn, client)

    assert client.sent == [update, PROBE_UPDATE, update]
    assert (result.outcome, result.done, result.updates_sent) == ("done", (pending.id,), 1)
    assert (result.failed_event, result.error, result.refusal) == (None, None, None)
    (row,) = await all_events(world.conn)
    assert (row["status"], row["attempts"], row["last_error"]) == ("done", 0, None)
    assert await watermark(world.conn) == ("number", str(pending.id))
    assert triples_of(client.store) == await world.library(record).expected()
    assert (await process_batch(world.conn, client)).outcome == "empty"


@pytest.mark.parametrize("where", ["probe", "second"])
@pytest.mark.parametrize("failure", TRANSIENT)
async def test_a_transient_failure_while_confirming_a_live_rejection_counts_nothing(
    world: World, failure: str | int, where: str
) -> None:
    _, ids, _ = await poisoned_third(world)
    before = rows_by_id(await all_events(world.conn))
    client = RecordingClient()
    struck = False

    async def behaviour(c: RecordingClient, sparql: str) -> None:
        nonlocal struck
        hit = sparql == PROBE_UPDATE if where == "probe" else PROBE_UPDATE in c.sent[:-1]
        if hit and not struck:
            struck = True
            raise transient(failure)
        if "Poison" in sparql:
            raise QLeverError("QLever refused", status=400, body="Invalid SPARQL")
        c.apply(sparql)

    client.behaviour = behaviour

    result = await process_batch(world.conn, client)

    assert struck and result.outcome == "unavailable"
    assert (result.refusal, result.failed_event, result.dead_lettered) == (None, None, False)
    assert result.done == tuple(ids[:2]) and result.updates_sent == 2
    rows = rows_by_id(await all_events(world.conn))
    for later in ids[2:]:
        assert rows[later] == before[later]
    assert {r[3] for i, r in rows.items() if i in ids[:2]} == {"done"}
    assert await watermark(world.conn) == ("number", str(ids[1]))

    client.behaviour = reject_containing("Poison")
    again = await process_batch(world.conn, client)

    assert (again.outcome, again.failed_event) == ("rejected", ids[2])
    assert (await event(world.conn, ids[2])).attempts == 1


async def test_a_refusal_keeps_the_attempts_already_counted(world: World) -> None:
    await world.new("Poison")
    await world.new("Later")
    poison, later = [e.id for e in await pending_events(world.conn)]
    client = RecordingClient()
    reject = reject_containing("Poison")

    async def refuse(c: RecordingClient, sparql: str) -> None:
        raise QLeverError("QLever refused", status=404, body="no such endpoint")

    async def poison_row() -> asyncpg.Record:
        return next(r for r in await all_events(world.conn) if r["id"] == poison)

    client.behaviour = reject
    for _ in range(2):
        assert (await process_batch(world.conn, client)).failed_event == poison
    counted = await poison_row()
    assert (counted["status"], counted["attempts"]) == ("pending", 2)

    client.behaviour = refuse
    for _ in range(3):
        result = await process_batch(world.conn, client)
        assert result.outcome == "unavailable" and result.refusal is not None
        assert tuple(await poison_row()) == tuple(counted)  # attempts 2, last_error unchanged

    client.behaviour = reject
    for attempt in (3, 4):
        assert not (await process_batch(world.conn, client)).dead_lettered
        assert (await poison_row())["attempts"] == attempt
    last = await process_batch(world.conn, client)
    assert last.dead_lettered and last.failed_event == poison
    assert ((await poison_row())["status"], (await poison_row())["attempts"]) == ("failed", 5)
    assert (await process_batch(world.conn, client)).done == (later,)


async def test_an_accepted_second_send_keeps_the_attempts_already_counted(world: World) -> None:
    """Test 60, continued (found by the §8.9 mutation run): ``attempts`` is never reset or
    lowered, also when the event is finally accepted on a second send (item 38)."""
    await world.new("Poison")
    await world.new("Later")
    poison, later = [e.id for e in await pending_events(world.conn)]
    client = RecordingClient()
    client.behaviour = reject_containing("Poison")
    for _ in range(2):
        assert (await process_batch(world.conn, client)).failed_event == poison
    counted = next(r for r in await all_events(world.conn) if r["id"] == poison)
    rejections = 0

    async def accepted_on_the_second_send(c: RecordingClient, sparql: str) -> None:
        nonlocal rejections
        if "Poison" in sparql and rejections < 2:  # the batch, then the poison alone
            rejections += 1
            raise QLeverError("QLever refused", status=400, body="not now")
        c.apply(sparql)

    client.behaviour = accepted_on_the_second_send

    result = await process_batch(world.conn, client)

    assert (result.outcome, result.done) == ("done", (poison, later))
    row = next(r for r in await all_events(world.conn) if r["id"] == poison)
    assert (row["status"], row["attempts"]) == ("done", 2)
    assert row["last_error"] == counted["last_error"]


@pytest.mark.parametrize(
    "case",
    ["healthy", "connect", "timeout", 500, 503, 401, 429, "combined only", "untranslatable"],
)
async def test_no_probe_is_sent_without_a_rejected_event(world: World, case: str | int) -> None:
    first = await world.new("Probe one")
    if case == "untranslatable":
        await insert_event(world.conn, first, "merge", 1, 2)
    await world.new("Probe two")
    await world.new("Probe three")
    client = RecordingClient()

    async def behaviour(c: RecordingClient, sparql: str) -> None:
        if case == "combined only" and len(c.sent) == 1:
            raise QLeverError("QLever refused", status=400, body="the batch only")
        if isinstance(case, int) or case in ("connect", "timeout"):
            raise transient(case)
        c.apply(sparql)

    client.behaviour = behaviour

    for _ in range(MAX_ATTEMPTS + 3):
        result = await process_batch(world.conn, client)

    assert PROBE_UPDATE not in client.sent
    if case in ("healthy", "combined only"):
        assert {row["status"] for row in await all_events(world.conn)} == {"done"}
    if case == "combined only":
        assert len(client.sent) == 4  # the batch, then each event alone
    if case == "untranslatable":
        assert result.outcome == "empty"


async def test_a_probe_is_never_counted_as_an_update_sent(
    world: World, db_url: str, slept: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test 61, continued: ``run`` with metrics on test 18's world."""
    await world.new("Poison")
    await world.new("Later")
    stop = asyncio.Event()
    client = RecordingClient()
    seen: list[Metrics] = []

    class Seen(Metrics):
        def __init__(self) -> None:
            super().__init__()
            seen.append(self)

    monkeypatch.setattr(sync_metrics, "Metrics", Seen)

    async def behaviour(c: RecordingClient, sparql: str) -> None:
        if "Poison" in sparql:
            raise QLeverError("QLever refused", status=400, body="no")
        c.apply(sparql)
        if "Later" in sparql:
            stop.set()

    client.behaviour = behaviour

    assert (
        await asyncio.wait_for(
            worker.run(worker_settings(db_url), client=client, stop=stop), timeout=30
        )
        == 0
    )

    (metrics,) = seen
    assert client.acknowledged.count(PROBE_UPDATE) == MAX_ATTEMPTS
    assert metrics.updates_sent_total == sum(s != PROBE_UPDATE for s in client.acknowledged) == 1
    assert metrics.sync_dead_letters_total == 1


@pytest.mark.parametrize("closed_during", ["the rejection", "the probe"])
async def test_nothing_is_sent_once_the_connection_closes_while_confirming(
    world: World,
    db_url: str,
    logged: Logged,
    monkeypatch: pytest.MonkeyPatch,
    closed_during: str,
) -> None:
    await world.new("Poison alone")
    before = await snapshot_tables(world.conn)
    connections: list[asyncpg.Connection] = []

    async def close_while_confirming(c: RecordingClient, sparql: str) -> None:
        hit = sparql == PROBE_UPDATE if closed_during == "the probe" else "Poison" in sparql
        if hit and not connections[-1].is_closed():
            await connections[-1].close()
        if "Poison" in sparql:
            raise QLeverError("QLever refused", status=400, body="Invalid SPARQL")
        c.apply(sparql)

    expected_sent = 1 if closed_during == "the rejection" else 2
    connections.append(await asyncpg.connect(asyncpg_dsn(db_url)))
    client = RecordingClient()
    client.behaviour = close_while_confirming

    with pytest.raises(ConnectionLostError):
        await process_batch(connections[-1], client)

    assert len(client.sent) == expected_sent
    assert await snapshot_tables(world.conn) == before

    real_connect = asyncpg.connect

    async def connect(*args: Any, **kwargs: Any) -> asyncpg.Connection:
        connection = await real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    running = RecordingClient()
    running.behaviour = close_while_confirming

    code = await asyncio.wait_for(
        worker.run(worker_settings(db_url), client=running, stop=asyncio.Event()), timeout=30
    )

    assert code == 1 and len(running.sent) == expected_sent
    events = logged()
    assert len(named(events, "sync_connection_lost")) == 1
    assert named(events, "sync_worker_failed") == []
    monkeypatch.undo()
    assert await snapshot_tables(world.conn) == before


# -- 71-72: the token is removed before a body is cut or encoded (contract §8.6) ----------------

STRADDLING_TOKEN = "secret-token-5f1c-abcdef"
"""At least 16 characters, with neither ``x`` nor ``y`` in it (test 71)."""

PLACES = [
    "live last_error",
    "catch-up last_error",
    "sync_event_failed",
    "sync_dead_letter live",
    "sync_dead_letter catch-up",
    "sync_updates_refused live",
    "sync_updates_refused catch-up",
    "Refusal.body",
]


async def _observe_body(
    world: World, db_url: str, place: str, answer: str, token: str, logged: Logged
) -> tuple[str, str]:
    """Make QLever answer ``answer`` and return the body as stored or logged at ``place``,
    and every text the worker produced there (rows, result, log)."""
    record = await world.new("Poison body")
    client = RecordingClient(token=token)
    reject_poison = reject_containing("Poison", body=answer)
    stop = asyncio.Event()

    async def refuse(c: RecordingClient, sparql: str) -> None:
        if sparql == PROBE_UPDATE:
            stop.set()
        raise QLeverError("QLever refused", status=404, body=answer)

    texts: list[str] = []
    catching_up = "catch-up" in place
    if catching_up:
        (pending,) = await pending_events(world.conn)
        assert (await process_batch(world.conn, RecordingClient())).outcome == "done"
        await set_watermark(world.conn, pending.id - 1)
    if place == "sync_dead_letter live":
        await world.conn.execute("UPDATE outbox_events SET attempts = $1", MAX_ATTEMPTS - 1)

    if place == "sync_updates_refused catch-up":
        client.behaviour = refuse
        await worker._catch_up_until_done(world.conn, client, stop)
    elif catching_up:
        client.behaviour = reject_poison
        texts.append(repr(await catch_up(world.conn, client)))
    elif place == "sync_updates_refused live":
        client.behaviour = refuse
        code = await asyncio.wait_for(
            worker.run(worker_settings(db_url), client=client, stop=stop), 30
        )
        assert code == 0
    else:
        client.behaviour = refuse if place == "Refusal.body" else reject_poison
        result = await process_batch(world.conn, client)
        texts.append(repr(result))
        if place == "Refusal.body":
            assert result.refusal is not None
            texts.append(result.refusal.body)
            return result.refusal.body, "\n".join(texts)

    rows = await all_events(world.conn)
    texts.extend(repr(tuple(row)) for row in rows)
    events = logged()
    texts.append(json.dumps(events))
    (row,) = [r for r in rows if r["record_id"] == record]
    if place in ("live last_error", "catch-up last_error"):
        return body_of(row["last_error"]), "\n".join(texts)
    name = place.split(" ")[0]
    lines = named(events, name)
    assert lines, (place, events)
    if name == "sync_updates_refused":
        return lines[0]["body"], "\n".join(texts)
    return body_of(lines[0]["error"]), "\n".join(texts)


@pytest.mark.parametrize("k", [8, len(STRADDLING_TOKEN) // 2, len(STRADDLING_TOKEN) - 1])
@pytest.mark.parametrize("place", PLACES)
async def test_a_token_that_straddles_the_cut_is_removed_whole(
    world: World, db_url: str, logged: Logged, slept: list[float], place: str, k: int
) -> None:
    assert len(STRADDLING_TOKEN) >= 16 and not {"x", "y"} & set(STRADDLING_TOKEN)
    answer = "x" * (2000 - k) + STRADDLING_TOKEN + "y" * 100

    body, everything = await _observe_body(world, db_url, place, answer, STRADDLING_TOKEN, logged)

    assert body == ("x" * (2000 - k) + "[redacted]" + "y" * 100)[:2000]
    assert STRADDLING_TOKEN[:8] not in everything


async def test_the_token_is_removed_from_an_unavailable_answer_in_catch_up_and_once(
    world: World, db_url: str, slept: list[float], logged: Logged
) -> None:
    """Item 44 for two lines the other token tests do not reach (added in review of #174):
    ``sync_unavailable`` during catch-up, and ``sync_worker_failed`` when ``--once`` meets an
    unavailable QLever in catch-up. QLever's 503 echoes the token."""
    await world.new("Token in a 503")
    (pending,) = await pending_events(world.conn)
    assert (await process_batch(world.conn, RecordingClient())).outcome == "done"
    await set_watermark(world.conn, pending.id - 1)
    client = RecordingClient()
    answers = 0

    async def busy(c: RecordingClient, sparql: str) -> None:
        nonlocal answers
        answers += 1
        if answers != 2:  # the second request, catch-up's retry, is acknowledged
            raise QLeverError("QLever busy", status=503, body=f"busy, said to {TOKEN}")
        c.apply(sparql)

    client.behaviour = busy

    await worker._catch_up_until_done(world.conn, client, asyncio.Event())

    assert await watermark(world.conn) == ("number", str(pending.id))
    await set_watermark(world.conn, pending.id - 1)
    assert await worker.run(worker_settings(db_url), once=True, client=client) == 1
    events = logged()
    [unavailable_line] = named(events, "sync_unavailable")
    assert unavailable_line["during"] == "catch_up" and "[redacted]" in unavailable_line["error"]
    [failed] = named(events, "sync_worker_failed")
    assert "[redacted]" in failed["error"] and failed["exit_code"] == 1
    assert (failed["persistent"], failed["command"]) == (False, "once")
    assert TOKEN not in json.dumps(events)


ESCAPING_TOKEN = 'tok"en-é-5f1c-0123456789'
"""Contains ``"`` and a non-ASCII character: ``EventError.__str__`` escapes both (test 72)."""


@pytest.mark.parametrize(
    "place",
    [
        "live last_error",
        "catch-up last_error",
        "sync_updates_refused live",
        "sync_dead_letter live",
    ],
)
async def test_a_token_that_json_escapes_is_removed(
    world: World, db_url: str, logged: Logged, slept: list[float], place: str
) -> None:
    answer = f"bad request with {ESCAPING_TOKEN} in it"

    body, everything = await _observe_body(world, db_url, place, answer, ESCAPING_TOKEN, logged)

    assert body == "bad request with [redacted] in it"
    escaped = json.dumps(ESCAPING_TOKEN)[1:-1]
    raw_escaped = json.dumps(ESCAPING_TOKEN, ensure_ascii=False)[1:-1]
    for form in (ESCAPING_TOKEN, escaped, raw_escaped, "5f1c-0123456789"):
        assert form not in everything, form
    for row in await all_events(world.conn):
        if row["last_error"]:
            assert "[redacted]" in json.loads(row["last_error"].split(" ", 1)[1])["body"]
