"""``relabel`` against real publishes (Step 1.19b, contract §5 tests 16-19; invariants 4 and 5).

The real ``api`` and an in-process real ``crdt-ws`` run on the test database, both built with
``label_language="fr"`` (the ``served`` and ``network`` fixtures of this module). Each record has
an English and a French primary name, and its ``entity_labels`` row is first set back to the
English label, as if it had been published under ``en``. Interleavings are forced by parking a
coroutine of ``lae.labels.relabel`` or of the publish service, and lock waits are confirmed with
``pg_stat_activity``, never with sleeps.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from pycrdt import Doc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import lae.labels.relabel as relabel_module
from lae.bootstrap.common import asyncpg_dsn
from lae.crdt.server import create_crdt_app
from lae.labels.upsert import upsert_entity_labels_for_record
from lae.projection import ops
from lae.publish import errors, service
from tests.api.conftest import (
    BASE_URI,
    CRDT_WS_URL,
    ENGLISH,
    FRENCH,
    INTERNAL_TOKEN,
    PRIMARY_NAME,
)
from tests.auth.conftest import Served, make_settings, serve_app
from tests.crdt.helpers import eventually
from tests.labels.relabel_support import empty_tables, execute, fetch, run
from tests.publish.conftest import Caller, Network, caller, edit, new_draft, publish, stored

LABEL_LANGUAGE = "fr"

WAITING_FOR_A_LOCK = text(
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
)


@pytest.fixture
async def clean(db_url: str, db_engine: AsyncEngine) -> AsyncIterator[None]:
    await empty_tables(db_url, db_engine)
    try:
        yield
    finally:
        await empty_tables(db_url, db_engine)


@pytest.fixture
async def network(clean: None, db_url: str) -> AsyncIterator[Network]:
    """``crdt-ws`` built with ``label_language="fr"`` (after the tables were emptied)."""
    crdt_app = create_crdt_app(
        make_settings(
            db_url,
            base_uri=BASE_URI,
            internal_token=INTERNAL_TOKEN,
            label_language=LABEL_LANGUAGE,
        )
    )
    async with crdt_app.router.lifespan_context(crdt_app):
        yield Network(crdt_app)


@pytest.fixture
async def served(db_url: str, network: Network) -> AsyncIterator[Served]:
    """``api`` built with ``label_language="fr"``, calling the ``crdt-ws`` of ``network``."""
    async with serve_app(
        db_url,
        http_transport=network,
        base_uri=BASE_URI,
        internal_token=INTERNAL_TOKEN,
        crdt_ws_url=CRDT_WS_URL,
        label_language=LABEL_LANGUAGE,
    ) as running:
        yield running


def bilingual(english: str, french: str) -> dict[str, Any]:
    """A ``Person`` template with an English and a French primary name, English first."""
    return {
        "identified_by": [
            {
                "type": "Name",
                "content": english,
                "classified_as": [PRIMARY_NAME],
                "language": [ENGLISH],
            },
            {
                "type": "Name",
                "content": french,
                "classified_as": [PRIMARY_NAME],
                "language": [FRENCH],
            },
        ]
    }


def rename_french(content: str) -> Callable[[Doc[Any]], None]:
    def change(doc: Doc[Any]) -> None:
        entry = ops.list_entries(doc, "identified_by")[1][1]
        ops.set_scalar(doc, "content", content, base=entry)

    return change


async def publish_accepting(app: Served, who: Caller, record_id: UUID) -> Any:
    """Publish, accepting whatever warnings the bundle has; return the final response."""
    response = await publish(app, who, record_id)
    if response.status_code == 409 and response.json().get("code") == errors.WARNINGS_NOT_ACCEPTED:
        shown = response.json()["params"]["records"]
        accepted = [
            {"code": w["code"], "path": w["path"], "record_id": rid}
            for rid, warnings in shown.items()
            for w in warnings
        ]
        response = await publish(app, who, record_id, *accepted)
    return response


async def published_person(
    served: Served, editor: Caller, english: str, french: str
) -> tuple[UUID, str]:
    """A published bilingual ``Person`` whose row then holds the English label."""
    record_id, uri = await new_draft(served, editor, "Person", bilingual(english, french))
    response = await publish_accepting(served, editor, record_id)
    assert response.status_code == 200, response.text
    assert await default_label(served_url(served), uri) == french  # published under fr
    await execute(
        served_url(served),
        "UPDATE entity_labels SET default_label = $2 WHERE uri = $1",
        uri,
        english,
    )
    return record_id, uri


def served_url(served: Served) -> str:
    url: str = served.app.state.settings.database_url
    return url


async def default_label(db_url: str, uri: str) -> str:
    [row] = await fetch(db_url, "SELECT default_label FROM entity_labels WHERE uri = $1", uri)
    label: str = row["default_label"]
    return label


async def lock_waiters(engine: AsyncEngine) -> int:
    async with AsyncSession(engine) as session:
        return int(await session.scalar(WAITING_FOR_A_LOCK) or 0)


def waiting(engine: AsyncEngine, count: int) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return await lock_waiters(engine) >= count

    return check


# --- 16: the record is read after the row is held -----------------------------------------------


async def test_relabel_reads_the_record_after_it_holds_the_row(
    served: Served,
    network: Network,
    db_url: str,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = await caller(db_engine)
    record_id, uri = await published_person(served, editor, "The Bridge", "Le Pont")
    lock = relabel_module._lock
    published: list[int] = []

    async def publish_then_lock(conn: Any, uris: Any, **options: Any) -> Any:
        if uri in uris and not published:
            # The batch transaction is open; a real publish renames the French name and commits.
            await edit(network, record_id, editor.id, rename_french("Le Pont Neuf"))
            response = await publish_accepting(served, editor, record_id)
            published.append(response.status_code)
        return await lock(conn, uris, **options)

    monkeypatch.setattr(relabel_module, "_lock", publish_then_lock)
    summary = await asyncio.wait_for(run(db_url, LABEL_LANGUAGE), 30)

    assert published == [200]
    assert summary["skipped_locked"] == 0
    assert await default_label(db_url, uri) == "Le Pont Neuf"
    after = await stored(db_engine, record_id)
    assert after.record is not None and after.record.version == 2


# --- 17: a publish waits for one batch, then wins ---


async def test_a_publish_waits_for_one_batch_and_then_wins(
    served: Served,
    network: Network,
    db_url: str,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = await caller(db_engine)
    record_id, uri = await published_person(served, editor, "The Bridge", "Le Pont")
    await edit(network, record_id, editor.id, rename_french("Le Pont Neuf"))
    before = await stored(db_engine, record_id)
    assert before.record is not None
    parked, release = asyncio.Event(), asyncio.Event()
    records = relabel_module._records

    async def parked_records(conn: Any, uris: Any) -> Any:
        if uri in uris and not parked.is_set():
            parked.set()
            await release.wait()
        return await records(conn, uris)

    monkeypatch.setattr(relabel_module, "_records", parked_records)
    relabelling = asyncio.create_task(run(db_url, LABEL_LANGUAGE))
    await asyncio.wait_for(parked.wait(), 10)
    publishing = asyncio.create_task(publish_accepting(served, editor, record_id))
    await eventually(waiting(db_engine, 1), seconds=20, what="the publish waits for the batch")
    assert not publishing.done()
    release.set()

    summary = await asyncio.wait_for(relabelling, 30)
    response = await asyncio.wait_for(publishing, 30)
    assert response.status_code == 200, response.text
    assert summary["changed"] == {"local": 1} and summary["skipped_locked"] == 0
    assert await default_label(db_url, uri) == "Le Pont Neuf"
    after = await stored(db_engine, record_id)
    assert after.record is not None and after.record.version == before.record.version + 1
    assert len(after.revisions) == len(before.revisions) + 1
    assert len(after.events) == len(before.events) + 1


# --- 18: a publish in flight defers the row -------------------------------------------------------


async def test_a_publish_in_flight_defers_its_row(
    served: Served,
    network: Network,
    db_url: str,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = await caller(db_engine)
    record_id, uri = await published_person(served, editor, "The Bridge", "Le Pont")
    other_id, other_uri = await published_person(served, editor, "The Tower", "La Tour")
    await edit(network, record_id, editor.id, rename_french("Le Pont Neuf"))
    upserted, release = asyncio.Event(), asyncio.Event()
    upsert = upsert_entity_labels_for_record

    async def upsert_then_park(*args: Any, **kwargs: Any) -> None:
        await upsert(*args, **kwargs)
        upserted.set()
        await release.wait()

    monkeypatch.setattr(service, "upsert_entity_labels_for_record", upsert_then_park)
    publishing = asyncio.create_task(publish_accepting(served, editor, record_id))
    await asyncio.wait_for(upserted.wait(), 20)

    summary = await asyncio.wait_for(run(db_url, LABEL_LANGUAGE), 30)
    assert summary["skipped_locked"] == 1
    assert summary["changed"] == {"local": 1}  # the other record
    assert await default_label(db_url, other_uri) == "La Tour"
    assert not publishing.done()

    release.set()
    response = await asyncio.wait_for(publishing, 30)
    assert response.status_code == 200, response.text
    assert await default_label(db_url, uri) == "Le Pont Neuf"
    assert (await stored(db_engine, other_id)).record is not None


# --- 19: no lock on records or the drafts ---


async def test_relabel_takes_no_lock_on_records(
    served: Served,
    network: Network,
    db_url: str,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = await caller(db_engine)
    record_id, uri = await published_person(served, editor, "The Bridge", "Le Pont")
    await edit(network, record_id, editor.id, rename_french("Le Pont Neuf"))  # a draft
    parked, release = asyncio.Event(), asyncio.Event()
    backend: list[int] = []
    apply = relabel_module._apply

    async def parked_apply(conn: Any, changes: Any) -> None:
        await apply(conn, changes)
        backend.append(conn.get_server_pid())
        parked.set()
        await release.wait()

    monkeypatch.setattr(relabel_module, "_apply", parked_apply)
    relabelling = asyncio.create_task(run(db_url, LABEL_LANGUAGE))
    try:
        await asyncio.wait_for(parked.wait(), 10)
        locks = await fetch(
            db_url,
            "SELECT locktype, relation::regclass::text AS relation, mode FROM pg_locks "
            "WHERE pid = $1",
            backend[0],
        )
        on_tables = {
            (row["relation"], row["mode"])
            for row in locks
            if row["relation"] in {"records", "crdt_drafts", "crdt_updates"}
        }
        # A plain read of records; no row lock (RowShareLock) and nothing on the drafts.
        assert on_tables == {("records", "AccessShareLock")}
        assert not [row for row in locks if row["locktype"] == "tuple"]
        assert ("entity_labels", "RowExclusiveLock") in {
            (row["relation"], row["mode"]) for row in locks
        }
        other = await asyncpg.connect(asyncpg_dsn(db_url))
        try:
            async with other.transaction():
                found = await other.fetch(
                    "SELECT id FROM records WHERE uri = $1 FOR UPDATE NOWAIT", uri
                )
                assert len(found) == 1
                drafts = await other.fetch(
                    "SELECT record_id FROM crdt_drafts WHERE record_id = $1 FOR UPDATE NOWAIT",
                    record_id,
                )
                assert len(drafts) == 1
        finally:
            await other.close()
    finally:
        release.set()
    summary = await asyncio.wait_for(relabelling, 30)
    assert summary["changed"] == {"local": 1}
    assert await default_label(db_url, uri) == "Le Pont"
