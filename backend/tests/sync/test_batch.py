"""Batching, acknowledgement, watermark, failures and the process (contract §3 items 10-24).

Tests 11-27 of the contract. The recording client applies each update to a ``pyoxigraph.Store``
standing in for QLever; ``store_matches`` checks it holds exactly the stored triples of the
records' last versions. Crashes are injected by the client; time by patching ``worker.pause``.
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
from uuid import UUID

import asyncpg
import httpx
import pyoxigraph
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from lae.bootstrap.common import asyncpg_dsn
from lae.logging import configure_logging
from lae.rdf import expand_to_ntriples
from lae.settings import Settings
from lae.sync import worker
from lae.sync.errors import REJECTED, UNTRANSLATABLE
from lae.sync.events import translate_event
from lae.sync.qlever_client import QLeverClient, QLeverError
from lae.sync.worker import (
    BATCH_SIZE,
    EXIT_CONFIG_ERROR,
    MAX_ATTEMPTS,
    SYNC_WORKER_LOCK,
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
    Network,
    RecordingClient,
    Served,
    all_events,
    copy_store,
    expected_store,
    id_of,
    insert_event,
    new_uri,
    operations,
    pending_events,
    person,
    publish_content,
    publish_edit,
    publish_new,
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


def reject_containing(marker: str, body: str = "Invalid SPARQL") -> Any:
    async def behaviour(c: RecordingClient, sparql: str) -> None:
        if marker in sparql:
            raise QLeverError("QLever refused", status=400, body=body)
        c.apply(sparql)

    return behaviour


async def test_a_rejected_batch_is_sent_event_by_event_and_only_the_bad_event_is_counted(
    world: World,
) -> None:
    await world.new("One")
    await world.new("Two")
    await world.new("Poison three")
    await world.new("Four")
    await world.new("Five")
    ids = [event.id for event in await pending_events(world.conn)]
    client = RecordingClient()
    client.behaviour = reject_containing("Poison", body="parse error near Poison")

    result = await process_batch(world.conn, client)

    assert result.outcome == "rejected"
    assert result.done == tuple(ids[:2]) and result.failed_event == ids[2]
    assert not result.dead_lettered and result.updates_sent == 2
    assert len(client.sent) == 4  # the batch, then events 1, 2 and 3 alone
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
    assert sum("Poison" in sparql for sparql in client.sent) == 2 * MAX_ATTEMPTS


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
    world: World, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QLever's answers echo the token here; the worker must not repeat it anywhere.

    The worker builds its client from the settings (``LAE_QLEVER_URL``,
    ``LAE_QLEVER_ACCESS_TOKEN``); only the HTTP transport is replaced.
    """

    async def pause(seconds: float, stop: asyncio.Event) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(worker, "pause", pause)
    await world.new("Token one")
    await world.new("Token two")
    answers = [503, 401, 400, 400]  # unavailable twice, then the batch and its first event
    stop = asyncio.Event()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        status = answers.pop(0) if answers else 200
        if status == 200:
            stop.set()
            return httpx.Response(200, json={"status": "OK"})
        echo = f"bad request with {request.headers['authorization']} and {TOKEN}"
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
    finally:
        root.handlers, root.level = saved
    assert code == 0
    assert set(seen) == {f"Bearer {TOKEN}"}
    logged = stream.getvalue()
    assert "sync_unavailable" in logged and "sync_event_failed" in logged
    errors = [row["last_error"] for row in await all_events(world.conn) if row["last_error"]]
    assert errors and all("[redacted]" in error for error in errors)
    assert TOKEN not in logged and all(TOKEN not in error for error in errors)
    from lae.sync.metrics import Metrics

    assert TOKEN not in Metrics().render()
