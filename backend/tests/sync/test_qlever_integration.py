"""The sync worker against a real QLever 0.6.0 (contract §5, tests 30-37; marked ``integration``).

Every test builds a fresh index with ``lae.bootstrap.index.build_index`` and ``deploy/Qleverfile``
in a temporary directory (empty, or from the N-Triples the test gives), serves it on
``LAE_TEST_QLEVER_PORT`` with a random access token, and stops exactly that server with
``stop_command(name, port)`` afterwards; never a bare ``qlever stop``, which stops every QLever on
the machine. Without ``qlever``, ``qlever-index`` or the port variable the tests skip; with
``LAE_REQUIRE_QLEVER=1`` (the ``sync-integration`` CI job) they fail instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import httpx
import orjson
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from lae.bootstrap.index import build_index, stop_command
from lae.corpus import corpus_files, load_record
from lae.rdf import canonical_ntriples, compress, expand_to_ntriples
from lae.settings import Settings
from lae.sparql.updates import build_update
from lae.sync import worker
from lae.sync.events import translate_event
from lae.sync.qlever_client import QLeverClient, QLeverError
from lae.sync.worker import classify, process_batch
from tests.api.conftest import AAT, insert_record, titled_record
from tests.publish.conftest import Caller, publish
from tests.publish.test_publish import set_bootstrap_versions
from tests.sync.conftest import (
    Network,
    RecordingClient,
    Served,
    id_of,
    insert_event,
    new_uri,
    pending_events,
    person,
    publish_content,
    publish_edit,
    publish_new,
    publish_ok,
    rename,
    revision_lines,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("queue")]

QLEVER_PORT_ENV = "LAE_TEST_QLEVER_PORT"
REQUIRE_ENV = "LAE_REQUIRE_QLEVER"
XSD = "http://www.w3.org/2001/XMLSchema#"
TEST_P = "https://x.test/"
CONTENT = "<http://www.cidoc-crm.org/cidoc-crm/P190_has_symbolic_content>"


def _missing(reason: str) -> None:
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"{reason} ({REQUIRE_ENV}=1)")
    pytest.skip(reason)


@dataclass
class Server:
    """One running QLever."""

    port: int
    token: str

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"

    def client(self) -> QLeverClient:
        return QLeverClient(self.url, self.url, self.token)

    def client_with(self, token: str | None) -> QLeverClient:
        return QLeverClient(self.url, self.url, token)

    async def count(self, where: str) -> int:
        async with self.client() as client:
            rows = await client.query(f"SELECT (COUNT(*) AS ?c) WHERE {{ {where} }}")
        value: int = rows[0]["c"]
        return value

    async def record_triples(self, uri: str) -> int:
        """Triples whose subject is the record or one of its nodes."""
        return await self.count(
            f'?s ?p ?o FILTER(?s = <{uri}> || STRSTARTS(STR(?s), "{uri}/node/"))'
        )

    async def delta_triples(self) -> float:
        async with httpx.AsyncClient() as http:
            response = await http.get(
                f"{self.url}/metrics", headers={"Authorization": f"Bearer {self.token}"}
            )
        response.raise_for_status()
        total = 0.0
        for line in response.text.splitlines():
            if line.startswith("qlever_delta_triples"):
                labels = line.split(" ")[0]
                if "{" not in labels or 'type="' not in labels:
                    total += float(line.rsplit(" ", 1)[1])
        return total


Start = Callable[..., Awaitable[Server]]


@pytest.fixture
async def start_qlever(db_url: str, tmp_path: Path) -> AsyncIterator[Start]:
    """``await start_qlever(lines=(), watermark=0)``: build an index of ``lines`` and serve it."""
    if shutil.which("qlever") is None or shutil.which("qlever-index") is None:
        _missing("QLever (qlever-control and qlever-index) is not installed")
    raw_port = os.environ.get(QLEVER_PORT_ENV)
    if not raw_port:
        _missing(f"{QLEVER_PORT_ENV} is not set (use your slot's QLever port)")
    port = int(str(raw_port))
    name = f"lae_sync_test_{port}"
    started: list[Path] = []

    async def start(lines: Iterable[str] = (), watermark: int = 0) -> Server:
        work = tmp_path / f"qlever{len(started)}"
        work.mkdir()
        nt = work / "input.nt"
        nt.write_bytes(canonical_ntriples(lines))
        token = secrets.token_hex(16)
        settings = Settings(database_url=db_url, qlever_access_token=SecretStr(token))
        started.append(work)
        await asyncio.to_thread(
            build_index,
            nt,
            settings=settings,
            index_dir=work / "index",
            name=name,
            port=port,
            watermark=watermark,
            ready_timeout_s=60,
        )
        return Server(port, token)

    try:
        yield start
    finally:
        for work in started:
            await asyncio.to_thread(
                subprocess.run,
                stop_command(name, port),
                cwd=work / "index",
                capture_output=True,
                check=False,
            )


# -- 30-31: the running loop and an edit --------------------------------------------------------


async def test_a_publish_is_in_qlever_within_a_second(
    served: Served, editor: Caller, db_url: str, start_qlever: Start
) -> None:
    """The worker as deployed: its client comes from the settings, with the access token."""
    server = await start_qlever()
    stop = asyncio.Event()
    settings = Settings(
        database_url=db_url,
        qlever_url=server.url,
        qlever_update_url=server.url,
        qlever_access_token=SecretStr(server.token),
        sync_metrics_port=None,
    )
    task = asyncio.create_task(worker.run(settings, stop=stop))
    try:
        await asyncio.sleep(0.5)  # the loop is idle, waiting for a notification
        _, uri = await publish_new(served, editor, person("Quickly Indexed"))
        published = time.monotonic()
        found = 0
        while time.monotonic() - published < 3 and not found:
            found = await server.record_triples(uri)
            if not found:
                await asyncio.sleep(0.02)
        elapsed = time.monotonic() - published
    finally:
        stop.set()
        assert await asyncio.wait_for(task, timeout=10) == 0
    assert found > 0
    assert elapsed < 1.0, elapsed
    print(f"published to found in QLever: {elapsed:.3f} s")


async def test_an_edit_replaces_exactly_the_changed_triple_in_qlever(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
    start_qlever: Start,
) -> None:
    server = await start_qlever()
    record_id, uri = await publish_new(served, editor, person("Night Watch", "Nachtwacht"))
    async with server.client() as client:
        assert (await process_batch(conn, client)).outcome == "done"
        v1 = await revision_lines(conn, record_id, 1)
        assert await server.record_triples(uri) == len(v1)
        await publish_edit(served, network, editor, db_engine, record_id, rename("The Night Watch"))
        assert (await process_batch(conn, client)).outcome == "done"
    v2 = await revision_lines(conn, record_id, 2)
    assert await server.record_triples(uri) == len(v2) == len(v1)
    assert await server.count(f'?s {CONTENT} "The Night Watch"') == 1
    assert await server.count(f'?s {CONTENT} "Night Watch"') == 0
    assert await server.count(f'?s {CONTENT} "Nachtwacht"') == 1


# -- 32-33: canonical lines delete what they inserted -------------------------------------------


def literal_lines(uri: str) -> list[str]:
    return [
        f'<{uri}> <{TEST_P}double> "2.5E0"^^<{XSD}double> .',
        f'<{uri}> <{TEST_P}double-small> "2.25E-12"^^<{XSD}double> .',
        f'<{uri}> <{TEST_P}integer> "7"^^<{XSD}integer> .',
        f'<{uri}> <{TEST_P}decimal> "2.50"^^<{XSD}decimal> .',
        f'<{uri}> <{TEST_P}dateTime> "1880-01-01T00:00:00Z"^^<{XSD}dateTime> .',
        f'<{uri}> <{TEST_P}lang> "Nachtwacht"@nl .',
    ]


async def insert_revision_lines(
    conn: asyncpg.Connection, record_id: UUID, version: int, data: Any, lines: Iterable[str]
) -> None:
    await conn.execute(
        "INSERT INTO record_revisions (record_id, version, ntriples_zstd, jsonld_zstd, "
        "context_version) VALUES ($1, $2, $3, $4, 1)",
        record_id,
        version,
        compress(canonical_ntriples(lines)),
        compress(orjson.dumps(data)),
    )


@pytest.mark.parametrize("inserted_by", ["update", "index build"])
async def test_canonical_doubles_are_deleted_from_qlever(
    db_engine: AsyncEngine, conn: asyncpg.Connection, start_qlever: Start, inserted_by: str
) -> None:
    """A record's typed and tagged literals, then an edit that removes them all.

    The decimal and the language tag are written by hand (the pinned context produces neither),
    in revisions inserted directly; the double, the integer and the dateTime are also what the
    expander writes (``"2.5E0"``, ``"7"``, an ``xsd:dateTime``).
    """
    uri = new_uri()
    data = titled_record(uri, "Literals", "Littéraux", f"{AAT}300033618")
    await insert_record(db_engine, data, version=2)
    record_id = id_of(uri)
    expanded = expand_to_ntriples(data, 1, record_uri=uri)
    assert any('"1880-01-01T00:00:00Z"^^' in line for line in expanded)
    literals = literal_lines(uri)
    v1 = frozenset(expanded) | frozenset(literals)
    v2 = frozenset(expanded) | {f'<{uri}> <{TEST_P}after> "edited" .'}
    await insert_revision_lines(conn, record_id, 1, data, v1)
    await insert_revision_lines(conn, record_id, 2, data, v2)
    if inserted_by == "update":
        server = await start_qlever()
        await insert_event(conn, record_id, "publish", None, 1)
    else:
        server = await start_qlever(v1)
    await insert_event(conn, record_id, "publish", 1, 2)
    literal_where = f'<{uri}> ?p ?o FILTER(STRSTARTS(STR(?p), "{TEST_P}"))'

    async with server.client() as client:
        if inserted_by == "update":
            assert len((await process_batch(conn, client, limit=1)).done) == 1
        assert await server.count(literal_where) == len(literals)
        assert await server.record_triples(uri) == len(v1)
        result = await process_batch(conn, client)

    assert result.outcome == "done"
    assert await server.count(literal_where) == 1  # only the line the edit added
    assert await server.count(f"<{uri}> <{TEST_P}double> ?o") == 0
    assert await server.record_triples(uri) == len(v2)


async def test_corpus_records_insert_and_retract_completely(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
    start_qlever: Start,
) -> None:
    """At least every sixth corpus record: first publish, an edit, a retract."""
    server = await start_qlever()
    checked = 0
    async with server.client() as client:
        for path in corpus_files()[::6]:
            template = load_record(path)
            created = await served.client.post(
                "/api/records",
                json={"type": template["type"], "template": template},
                headers=editor.headers,
            )
            if created.status_code == 422:
                continue
            record_id, uri = UUID(created.json()["id"]), created.json()["uri"]
            response = await publish(served, editor, record_id)
            if response.status_code == 422:
                continue
            if response.status_code != 200:
                await publish_ok(served, editor, record_id)
            assert (await process_batch(conn, client)).outcome == "done"
            v1 = await revision_lines(conn, record_id, 1)
            assert await server.record_triples(uri) == len(v1), path

            def edit(data: dict[str, Any]) -> None:
                data["_label"] = f"{data.get('_label', '')} (edited)"
                if data.get("identified_by"):
                    data["identified_by"].pop()  # removes a node, or a reference

            await publish_edit(served, network, editor, db_engine, record_id, edit)
            assert (await process_batch(conn, client)).outcome == "done"
            v2 = await revision_lines(conn, record_id, 2)
            assert await server.record_triples(uri) == len(v2), path

            await insert_event(conn, record_id, "delete", 2, None)
            assert (await process_batch(conn, client)).outcome == "done"
            assert await server.record_triples(uri) == 0, path
            checked += 1
    assert checked >= 25, checked


# -- 34-35: drift and repetition ----------------------------------------------------------------


async def test_drift_fallback_removes_triples_the_stored_revision_does_not_list(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
    start_qlever: Start,
) -> None:
    await set_bootstrap_versions(db_engine, None, None)
    uri = new_uri()
    loaded = titled_record(uri, "Drifted", "Dérivé", f"{AAT}300033618")
    node = loaded["identified_by"][1]["id"]
    extra = f'<{node}> <{TEST_P}from-an-older-expander> "drift" .'
    server = await start_qlever([*expand_to_ntriples(loaded, 1, record_uri=uri), extra])
    await insert_record(db_engine, loaded, version=1)
    record_id = id_of(uri)
    assert await server.count(f"<{node}> <{TEST_P}from-an-older-expander> ?o") == 1
    edited = json.loads(json.dumps(loaded))
    edited["identified_by"][0]["content"] = "Drifted, edited"
    await publish_content(served, network, editor, record_id, edited)
    (pending,) = await pending_events(conn)
    assert pending.drift_check

    async with server.client() as client:
        assert (await process_batch(conn, client)).outcome == "done"

    v2 = await revision_lines(conn, record_id, 2)
    assert await server.count(f"<{node}> <{TEST_P}from-an-older-expander> ?o") == 0
    assert await server.record_triples(uri) == len(v2)
    assert await server.count('?s ?p "Drifted, edited"') == 1


class LostAnswer(QLeverClient):
    """Sends the first update to QLever, then reports a lost answer; later ones go through."""

    lost = False

    async def update(self, sparql: str) -> None:
        await super().update(sparql)
        if not self.lost:
            self.lost = True
            raise QLeverError("the answer was lost", body="ReadTimeout")


async def test_sending_a_batch_twice_leaves_qlever_as_after_once(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
    start_qlever: Start,
) -> None:
    server = await start_qlever()
    record_id, uri = await publish_new(served, editor, person("Added", "Removed by the edit"))

    def drop(data: dict[str, Any]) -> None:
        del data["identified_by"][1]

    await publish_edit(served, network, editor, db_engine, record_id, drop)
    ids = [event.id for event in await pending_events(conn)]
    async with LostAnswer(server.url, server.url, server.token) as client:
        assert (await process_batch(conn, client)).outcome == "unavailable"
        assert await server.count(f'?s {CONTENT} "Removed by the edit"') == 0  # applied once
        assert (await process_batch(conn, client)).done == tuple(ids)

    v2 = await revision_lines(conn, record_id, 2)
    assert await server.record_triples(uri) == len(v2)
    assert await server.count(f'?s {CONTENT} "Removed by the edit"') == 0
    assert await server.count(f'?s {CONTENT} "Added"') == 1


# -- 36-37: what QLever answers -----------------------------------------------------------------


async def test_qlever_refuses_an_update_without_the_token_and_a_malformed_one(
    start_qlever: Start,
) -> None:
    server = await start_qlever()
    update = build_update([], [f'<{TEST_P}s> <{TEST_P}p> "o" .'])
    statuses: dict[str, int | None] = {}
    for case, client in (
        ("no token", server.client_with(None)),
        ("wrong token", server.client_with("not-the-token")),
    ):
        async with client:
            with pytest.raises(QLeverError) as raised:
                await client.update(update)
        statuses[case] = raised.value.status
        assert raised.value.status in (401, 403), raised.value
        assert classify(raised.value) == "unavailable"
    async with server.client() as client:
        with pytest.raises(QLeverError) as raised:
            await client.update(f"INSERT DATA {{ <{TEST_P}s> <{TEST_P}p> }}")
        statuses["syntax error"] = raised.value.status
        assert raised.value.status is not None and 400 <= raised.value.status < 500
        assert classify(raised.value) == "rejected"
        await client.update(update)  # the token works
    assert await server.count(f'<{TEST_P}s> <{TEST_P}p> "o"') == 1
    print(f"QLever statuses: {statuses}")


async def test_delta_triples_grow_by_the_gross_change(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
    start_qlever: Start,
) -> None:
    """The record's version 1 is in the index as built, as after a bootstrap (Spike S1 §3).

    Changing a triple that an earlier update inserted moves it from "inserted" to "deleted" in
    QLever 0.6.0 and adds only 1 (measured; ``docs/questions.md``, Step 1.11).
    """
    record_id, _ = await publish_new(served, editor, person("Delta"))
    exported = RecordingClient()  # the first event is in the export the index is built from
    assert (await process_batch(conn, exported)).outcome == "done"
    server = await start_qlever(await revision_lines(conn, record_id, 1))
    async with server.client() as client:
        before = await server.delta_triples()
        await publish_edit(served, network, editor, db_engine, record_id, rename("Delta two"))
        (pending,) = await pending_events(conn)
        update = await translate_event(conn, pending)
        assert (await process_batch(conn, client)).outcome == "done"
        after = await server.delta_triples()
        await client.update(update)  # the same update again, as a replay would send it
        replayed = await server.delta_triples()
    assert before == 0
    assert after - before == 2
    assert replayed >= after
