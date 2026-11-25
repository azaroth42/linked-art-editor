"""The draft reaper (``lae.scheduler.reaper``): idle drafts go, never-published records with them.

The three cases of the guide (Step 1.15, item 4): an idle draft of a published record is
discarded and the record stays; an idle draft of a never-published record takes the record with
it; ``POST /api/records/{id}/draft/extend`` keeps a draft. Then the edges: abandoned creations,
``crdt-ws`` down, a draft that comes back to life between listing and discard, errors, shutdown.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from uuid_utils.compat import uuid7

from lae.crdt.client import CrdtWsClient
from lae.projection import ops
from lae.scheduler import reaper
from lae.scheduler.jobs import JobContext
from lae.scheduler.reaper import (
    IdleDraft,
    ReapAction,
    list_idle_drafts,
    reap_draft,
    reap_drafts,
)
from tests.api.conftest import (
    CRDT_WS_URL,
    INTERNAL_TOKEN,
    Network,
    id_of,
    insert_draft,
    insert_record,
    new_uri,
    titled_record,
)
from tests.auth.conftest import Served, bearer, create_token, create_user, make_settings
from tests.crdt.helpers import change, replica_of
from tests.scheduler.conftest import TTL_DAYS, draft_row, make_idle, record_row

TYPE_URI = "http://vocab.getty.edu/aat/300033618"


async def published_record_with_draft(network: Network, engine: AsyncEngine) -> UUID:
    """A published (version 1) record whose draft is open in ``crdt-ws``."""
    uri = new_uri()
    await insert_record(engine, titled_record(uri, "Idle object", "Objet inactif", TYPE_URI))
    record_id = id_of(uri)
    await network.rooms.open(record_id)
    return record_id


async def created_record(served: Served, engine: AsyncEngine) -> tuple[UUID, dict[str, str]]:
    """A never-published record made through ``POST /api/records`` (draft open); its headers."""
    user = await create_user(engine, "Collaborator")
    token, _ = await create_token(engine, user, "edit_draft")
    headers = bearer(token)
    response = await served.client.post("/api/records", json={"type": "Person"}, headers=headers)
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"]), headers


async def test_idle_draft_of_a_published_record_is_discarded(
    network: Network,
    db_engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    crdt: CrdtWsClient,
) -> None:
    record_id = await published_record_with_draft(network, db_engine)
    before = await record_row(db_engine, record_id)
    assert before is not None
    contributors = [uuid7(), uuid7()]
    await make_idle(db_engine, record_id, contributors=contributors)

    with structlog.testing.capture_logs() as logs:
        result = await reap_drafts(sessions, crdt, TTL_DAYS)

    assert record_id in result.discarded
    assert record_id not in result.deleted
    assert await draft_row(db_engine, record_id) is None
    assert not await network.rooms.is_open(record_id)
    after = await record_row(db_engine, record_id)
    assert after is not None
    assert (after.version, after.data) == (1, before.data)
    assert after.draft_epoch == before.draft_epoch + 1  # discarding a draft ends its epoch
    (event,) = [
        log for log in logs if log["event"] == "draft_reaped" and log["record_id"] == str(record_id)
    ]
    assert event["action"] == "discarded"
    assert event["contributors"] == [str(user) for user in contributors]
    assert event["via"] == "crdt-ws"


async def test_idle_draft_of_a_never_published_record_takes_the_record(
    served: Served,
    network: Network,
    db_engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    crdt: CrdtWsClient,
) -> None:
    record_id, _ = await created_record(served, db_engine)
    assert await network.rooms.is_open(record_id)
    await make_idle(db_engine, record_id)

    with structlog.testing.capture_logs() as logs:
        result = await reap_drafts(sessions, crdt, TTL_DAYS)

    assert record_id in result.deleted
    assert record_id not in result.discarded
    assert await record_row(db_engine, record_id) is None
    assert await draft_row(db_engine, record_id) is None
    assert not await network.rooms.is_open(record_id)
    (event,) = [
        log for log in logs if log["event"] == "draft_reaped" and log["record_id"] == str(record_id)
    ]
    assert event["action"] == ReapAction.RECORD_DELETED.value


async def test_extend_keeps_a_draft_from_the_reaper(
    served: Served,
    network: Network,
    db_engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    crdt: CrdtWsClient,
) -> None:
    record_id, headers = await created_record(served, db_engine)
    await make_idle(db_engine, record_id)

    response = await served.client.post(f"/api/records/{record_id}/draft/extend", headers=headers)
    assert response.status_code == 200, response.text
    result = await reap_drafts(sessions, crdt, TTL_DAYS)

    assert record_id not in result.deleted + result.discarded + result.failed
    assert await draft_row(db_engine, record_id) is not None
    assert await record_row(db_engine, record_id) is not None
    assert await network.rooms.is_open(record_id)


async def test_draft_within_the_ttl_is_left_alone(
    network: Network,
    db_engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    crdt: CrdtWsClient,
) -> None:
    record_id = await published_record_with_draft(network, db_engine)
    await make_idle(db_engine, record_id, days=TTL_DAYS - 0.5)
    result = await reap_drafts(sessions, crdt, TTL_DAYS)
    assert record_id not in result.discarded + result.skipped
    assert await draft_row(db_engine, record_id) is not None


async def test_abandoned_creation_without_a_draft_is_deleted(
    db_engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], crdt: CrdtWsClient
) -> None:
    old, recent, published = new_uri(), new_uri(), new_uri()
    for uri, version in ((old, 0), (recent, 0), (published, 1)):
        data = titled_record(uri, "Stub", "Ébauche", TYPE_URI)
        await insert_record(db_engine, data, version=version)
    await make_idle(db_engine, id_of(old))
    await make_idle(db_engine, id_of(published))

    result = await reap_drafts(sessions, crdt, TTL_DAYS)

    assert id_of(old) in result.deleted
    assert await record_row(db_engine, id_of(old)) is None
    assert await record_row(db_engine, id_of(recent)) is not None  # created within the TTL
    assert await record_row(db_engine, id_of(published)) is not None  # published: never reaped


async def test_without_crdt_ws_the_draft_is_deleted_in_the_database(
    network: Network,
    db_engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    crdt: CrdtWsClient,
) -> None:
    published, unpublished = new_uri(), new_uri()
    await insert_record(db_engine, titled_record(published, "A", "A", TYPE_URI))
    await insert_record(db_engine, titled_record(unpublished, "B", "B", TYPE_URI), version=0)
    for uri in (published, unpublished):
        await insert_draft(db_engine, id_of(uri))
        await make_idle(db_engine, id_of(uri))
    network.crdt_down = True

    with structlog.testing.capture_logs() as logs:
        result = await reap_drafts(sessions, crdt, TTL_DAYS)

    assert id_of(published) in result.discarded
    assert id_of(unpublished) in result.deleted
    assert await draft_row(db_engine, id_of(published)) is None
    kept = await record_row(db_engine, id_of(published))
    assert kept is not None and kept.draft_epoch == 1  # through store.delete_draft
    assert await record_row(db_engine, id_of(unpublished)) is None
    vias = {log["record_id"]: log["via"] for log in logs if log["event"] == "draft_reaped"}
    assert vias[str(id_of(published))] == "database"


async def test_a_draft_active_again_since_the_listing_is_skipped(
    network: Network,
    db_engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    crdt: CrdtWsClient,
) -> None:
    record_id = await published_record_with_draft(network, db_engine)
    await make_idle(db_engine, record_id)
    ttl = timedelta(days=TTL_DAYS)
    (listed,) = [d for d in await list_idle_drafts(sessions, ttl) if d.record_id == record_id]
    async with AsyncSession(db_engine) as session, session.begin():
        await session.execute(
            text("UPDATE crdt_drafts SET last_activity_at = now() WHERE record_id = :id"),
            {"id": record_id},
        )

    assert await reap_draft(sessions, crdt, listed, ttl) is None
    network.crdt_down = True  # the database path checks again under its locks
    assert await reap_draft(sessions, crdt, listed, ttl) is None
    assert await draft_row(db_engine, record_id) is not None


class OvertakenClient(CrdtWsClient):
    """A ``crdt-ws`` client whose ``close`` is overtaken by something that touches the draft.

    ``before_close`` runs after the reaper has decided to discard the draft (its last re-check)
    and before ``crdt-ws`` handles the close: the window of ADR-0029.
    """

    before_close: Any = None

    async def close(self, record_id: UUID, **options: Any) -> None:
        await self.before_close()
        await super().close(record_id, **options)


async def test_an_edit_that_overtakes_the_discard_call_is_not_lost(
    network: Network, db_engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    # Core review A, finding 1 (#178): the reaper re-checks idleness, then asks crdt-ws to
    # discard. An edit accepted by crdt-ws in between used to be deleted with the draft.
    record_id = await published_record_with_draft(network, db_engine)
    editor = await create_user(db_engine, "Editor")
    await make_idle(db_engine, record_id)
    ttl = timedelta(days=TTL_DAYS)
    (listed,) = [d for d in await list_idle_drafts(sessions, ttl) if d.record_id == record_id]
    room = await network.rooms.get(record_id)
    assert room is not None

    async def edit() -> None:
        payload = change(
            replica_of(room.ydoc),
            lambda doc: ops.add_node(doc, "identified_by", {"type": "Name", "content": "Late"}),
        )
        await network.rooms.apply_update(record_id, payload, editor)

    async with httpx.AsyncClient(transport=network) as http:
        overtaken = OvertakenClient(http, CRDT_WS_URL, INTERNAL_TOKEN)
        overtaken.before_close = edit
        assert await reap_draft(sessions, overtaken, listed, ttl) is None  # skipped

    row = await draft_row(db_engine, record_id)
    assert row is not None and row.draft_epoch == listed.draft_epoch
    assert await network.rooms.get(record_id) is room  # the room was not retired either
    snapshot = await network.rooms.snapshot(record_id)
    assert "Late" in [name["content"] for name in snapshot.jsonld["identified_by"]]
    assert snapshot.contributors == [editor]
    await network.rooms.close(record_id, discard=True)


async def test_an_extend_that_overtakes_the_discard_call_keeps_the_draft(
    served: Served,
    network: Network,
    db_engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    record_id, headers = await created_record(served, db_engine)
    await make_idle(db_engine, record_id)
    ttl = timedelta(days=TTL_DAYS)
    (listed,) = [d for d in await list_idle_drafts(sessions, ttl) if d.record_id == record_id]

    async def extend() -> None:
        response = await served.client.post(
            f"/api/records/{record_id}/draft/extend", headers=headers
        )
        assert response.status_code == 200, response.text

    async with httpx.AsyncClient(transport=network) as http:
        overtaken = OvertakenClient(http, CRDT_WS_URL, INTERNAL_TOKEN)
        overtaken.before_close = extend
        assert await reap_draft(sessions, overtaken, listed, ttl) is None

    assert await draft_row(db_engine, record_id) is not None
    assert await record_row(db_engine, record_id) is not None  # version 0: not deleted either
    assert await network.rooms.is_open(record_id)


async def test_an_idle_draft_that_crdt_ws_keeps_is_a_failure(
    network: Network, db_engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    # crdt-ws answers 204 whether or not it discarded; the reaper must not take a draft that
    # is still there and still idle for a discarded one.
    record_id = await published_record_with_draft(network, db_engine)
    await make_idle(db_engine, record_id)
    ttl = timedelta(days=TTL_DAYS)
    (listed,) = [d for d in await list_idle_drafts(sessions, ttl) if d.record_id == record_id]

    class Deaf(CrdtWsClient):
        async def close(self, record_id: UUID, **options: Any) -> None:
            return None

    async with httpx.AsyncClient(transport=network) as http:
        with pytest.raises(RuntimeError):
            await reap_draft(sessions, Deaf(http, CRDT_WS_URL, INTERNAL_TOKEN), listed, ttl)
    await network.rooms.close(record_id, discard=True)


async def test_a_draft_replaced_since_the_listing_is_skipped(
    network: Network,
    db_engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    crdt: CrdtWsClient,
) -> None:
    record_id = await published_record_with_draft(network, db_engine)
    await make_idle(db_engine, record_id)
    row = await draft_row(db_engine, record_id)
    assert row is not None
    stale = IdleDraft(record_id, row.draft_epoch - 1, 1, (), datetime(2026, 1, 1, tzinfo=UTC))
    assert await reap_draft(sessions, crdt, stale, timedelta(days=TTL_DAYS)) is None
    assert await draft_row(db_engine, record_id) is not None


async def test_a_failure_is_counted_and_the_pass_goes_on(
    network: Network, db_engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    first = await published_record_with_draft(network, db_engine)
    second = await published_record_with_draft(network, db_engine)
    for record_id in (first, second):
        await make_idle(db_engine, record_id)
    async with httpx.AsyncClient(transport=network) as http:
        refused = CrdtWsClient(http, CRDT_WS_URL, "not-the-internal-token")
        result = await reap_drafts(sessions, refused, TTL_DAYS)
    assert {first, second} <= set(result.failed)
    assert await draft_row(db_engine, first) is not None
    assert await draft_row(db_engine, second) is not None
    await network.rooms.close(first, discard=True)  # not left idle for the other tests
    await network.rooms.close(second, discard=True)


async def test_shutdown_stops_the_pass_between_drafts(
    network: Network,
    db_engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    crdt: CrdtWsClient,
) -> None:
    record_id = await published_record_with_draft(network, db_engine)
    await make_idle(db_engine, record_id)
    result = await reap_drafts(sessions, crdt, TTL_DAYS, should_stop=lambda: True)
    assert result.interrupted
    assert result.summary()["interrupted"] is True
    assert result.discarded == result.deleted == []
    assert await draft_row(db_engine, record_id) is not None
    await reap_drafts(sessions, crdt, TTL_DAYS)  # not left idle for the other tests


async def test_a_ttl_below_one_day_is_refused(
    sessions: async_sessionmaker[AsyncSession], crdt: CrdtWsClient
) -> None:
    with pytest.raises(ValueError, match="draft_ttl_days"):
        await reap_drafts(sessions, crdt, 0)


async def test_the_job_runs_one_pass_with_the_settings_ttl(
    db_url: str,
    network: Network,
    db_engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    record_id = await published_record_with_draft(network, db_engine)
    await make_idle(db_engine, record_id, days=3)
    settings = make_settings(
        db_url, draft_ttl_days=2, crdt_ws_url=CRDT_WS_URL, internal_token=INTERNAL_TOKEN
    )
    async with httpx.AsyncClient(transport=network) as http:
        ctx = JobContext(settings=settings, engine=db_engine, sessions=sessions, http=http)
        summary = await reaper.run(ctx)
    assert summary["discarded"] >= 1
    assert set(summary) == {"discarded", "deleted", "skipped", "failed", "interrupted"}
    assert await draft_row(db_engine, record_id) is None
