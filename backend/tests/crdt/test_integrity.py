"""Interleavings between a room and other transactions (the Step 1.8 review's findings).

Each test is one race that used to lose or corrupt something: a publish committing while a room
is being loaded, a publish and a write locking in opposite orders, a compaction hiding an edit
from the publish that follows a snapshot, a stale client creating a draft, and compaction
running while updates keep arriving.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.crdt import store
from lae.crdt.rooms import (
    DraftSupersededError,
    EpochMismatchError,
    InMemoryRoomManager,
)
from lae.db.models import CrdtUpdate, Record
from lae.projection import ops, project
from tests.crdt.helpers import (
    change,
    crash,
    create_record,
    draft_row,
    record_epoch,
    replica_of,
    sessions_of,
    update_count,
)


def manager_for(engine: AsyncEngine, **options: Any) -> InMemoryRoomManager:
    return InMemoryRoomManager(sessions_of(engine), **options)


def add_name(content: str) -> Any:
    return lambda doc: ops.add_node(doc, "identified_by", {"type": "Name", "content": content})


def names(jsonld: dict[str, Any]) -> list[str]:
    return [name["content"] for name in jsonld["identified_by"]]


MARKER = "x_published"
"""A key the publish below sets: which version a draft was hydrated from. (Not ``_label``, which
a snapshot derives from the names since Step 1.19a.)"""


async def publish(session: AsyncSession, record_id: UUID, label: str) -> None:
    """The draft-related part of Step 1.10's transaction, in its order; the caller commits."""
    record = (
        await session.execute(select(Record).where(Record.id == record_id).with_for_update())
    ).scalar_one()
    await session.execute(
        update(Record)
        .where(Record.id == record_id)
        .values(
            data={**record.data, MARKER: label},
            version=Record.version + 1,
            draft_epoch=Record.draft_epoch + 1,
        )
    )
    await store.delete_draft(session, record_id)


async def test_a_publish_cannot_commit_between_the_two_reads_of_a_load(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 1. Without the lock on ``records`` the room below was a second hydration at
    epoch 0, built from the data of version 1 after version 2 had been published."""
    record_id, _ = await create_record(db_engine, version=1, draft_epoch=0)
    sessions = sessions_of(db_engine)
    publishing: list[asyncio.Task[None]] = []
    real_load_draft = store.load_draft

    async def publish_now() -> None:
        async with sessions() as session, session.begin():
            await publish(session, record_id, "Published meanwhile")

    async def load_draft_after_a_publish_started(*args: Any, **kwargs: Any) -> Any:
        # The room has read the record; a publish starts before it reads the draft.
        publishing.append(asyncio.create_task(publish_now()))
        await asyncio.sleep(0.3)
        assert not publishing[0].done(), "the publish must wait for the load"
        return await real_load_draft(*args, **kwargs)

    monkeypatch.setattr(store, "load_draft", load_draft_after_a_publish_started)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        monkeypatch.undo()
        # The load won: a consistent draft of version 1 at epoch 0 ...
        assert (room.draft_epoch, room.base_version) == (0, 1)
        assert MARKER not in room.snapshot().jsonld
        # ... which the publish, queued behind it, then cleared.
        await asyncio.wait_for(publishing[0], 5)
        assert await record_epoch(db_engine, record_id) == 1
        assert await draft_row(db_engine, record_id) is None
        fresh = await manager.open(record_id)
        assert fresh is not room
        assert (fresh.draft_epoch, fresh.base_version) == (1, 2)
        assert fresh.snapshot().jsonld[MARKER] == "Published meanwhile"


async def test_a_write_during_a_publish_waits_and_is_refused_without_a_deadlock(
    db_engine: AsyncEngine,
) -> None:
    """Lock order. The write used to lock the draft row and then wait for the record row, while
    the publish held the record row and waited for the draft row: PostgreSQL killed one."""
    record_id, _ = await create_record(db_engine)
    sessions = sessions_of(db_engine)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        payload = change(replica_of(room.ydoc), add_name("During the publish"))
        async with sessions() as session:
            await session.execute(select(Record.id).where(Record.id == record_id).with_for_update())
            writing = asyncio.create_task(room.write(payload, None))
            await asyncio.sleep(0.3)
            assert not writing.done(), "the write waits for the publish"
            await publish(session, record_id, "Published")
            await session.commit()
        with pytest.raises(DraftSupersededError):
            await asyncio.wait_for(writing, 5)
        assert await update_count(db_engine, record_id) == 0
        assert names(project(room.ydoc)) == ["First name"]


async def test_a_publish_waits_for_a_write_in_flight(db_engine: AsyncEngine) -> None:
    """The other order: the write has its locks, the publish queues behind it and then sees the
    update in ``last_update_id``."""
    record_id, _ = await create_record(db_engine)
    sessions = sessions_of(db_engine)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        before = room.snapshot()
        payload = change(replica_of(room.ydoc), add_name("Just in time"))
        async with sessions() as writer:
            await store.append_update(writer, record_id, 0, payload, None)  # not committed yet

            async def publish_if_unchanged() -> bool:
                async with sessions() as session, session.begin():
                    await session.execute(
                        select(Record.id).where(Record.id == record_id).with_for_update()
                    )
                    return await store.draft_unchanged(
                        session, record_id, before.draft_epoch, before.last_update_id
                    )

            publishing = asyncio.create_task(publish_if_unchanged())
            await asyncio.sleep(0.3)
            assert not publishing.done(), "the publish waits for the write"
            await writer.commit()
        assert await asyncio.wait_for(publishing, 5) is False


async def test_last_update_id_tells_a_publish_about_edits_that_compaction_folded_away(
    db_engine: AsyncEngine,
) -> None:
    """A snapshot, then an edit that triggers compaction, then the publish's check.

    ``max(crdt_updates.id)`` no longer shows the edit (compaction deleted the rows), so a
    publish relying on it would have deleted the draft with the edit in it.
    """
    record_id, _ = await create_record(db_engine)
    sessions = sessions_of(db_engine)
    async with manager_for(db_engine, compact_every=2) as manager:
        room = await manager.open(record_id)
        assert room.snapshot().last_update_id == 0
        await manager.apply_update(record_id, change(replica_of(room.ydoc), add_name("A")), None)
        validated = await manager.snapshot(record_id)  # what the publisher validated
        assert validated.last_update_id > 0

        async def unchanged() -> bool:
            async with sessions() as session, session.begin():
                return await store.draft_unchanged(
                    session, record_id, validated.draft_epoch, validated.last_update_id
                )

        assert await unchanged()
        await manager.apply_update(
            record_id, change(replica_of(room.ydoc), add_name("After the snapshot")), None
        )
        assert await update_count(db_engine, record_id) == 0  # compacted: no row tells
        async with AsyncSession(db_engine) as session:
            newest = select(func.max(CrdtUpdate.id)).where(CrdtUpdate.record_id == record_id)
            assert await session.scalar(newest) is None
        assert not await unchanged()
        current = await manager.snapshot(record_id)
        assert current.last_update_id > validated.last_update_id
        row = await draft_row(db_engine, record_id)
        assert row is not None and row.last_update_id == current.last_update_id

        # Eviction and reload change nothing about the draft, so nothing about its version.
        await manager.close(record_id, discard=False)
        assert (await manager.snapshot(record_id)).last_update_id == current.last_update_id
        assert not await unchanged()
        async with sessions() as session, session.begin():
            assert await store.draft_unchanged(session, record_id, 0, current.last_update_id)
            assert not await store.draft_unchanged(session, record_id, 1, current.last_update_id)


async def test_a_caller_with_another_epoch_gets_no_room_and_creates_no_draft(
    db_engine: AsyncEngine,
) -> None:
    record_id, _ = await create_record(db_engine, version=2, draft_epoch=3)
    async with manager_for(db_engine) as manager:
        for stale in (2, 4):
            with pytest.raises(EpochMismatchError) as mismatch:
                await manager.open(record_id, expected_epoch=stale)
            assert mismatch.value.current == 3
        assert await draft_row(db_engine, record_id) is None
        assert not await manager.is_open(record_id)

        room = await manager.open(record_id, expected_epoch=3)
        assert await manager.open(record_id, expected_epoch=3) is room
        with pytest.raises(EpochMismatchError):  # the room is in memory
            await manager.open(record_id, expected_epoch=2)
        await manager.close(record_id, discard=False)
        with pytest.raises(EpochMismatchError):  # the draft is only in the database
            await manager.open(record_id, expected_epoch=2)
        assert not await manager.is_open(record_id)


async def test_delete_draft_of_a_missing_record_is_a_no_op(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    sessions = sessions_of(db_engine)
    async with sessions() as session, session.begin():
        assert await store.delete_draft(session, record_id) == 0  # no draft: epoch unchanged
    async with sessions() as session, session.begin():
        await session.execute(update(Record).where(Record.id == record_id).values(draft_epoch=5))
    async with manager_for(db_engine) as manager:
        await manager.open(record_id)
        await crash(manager)
    async with sessions() as session, session.begin():
        assert await store.delete_draft(session, record_id) == 6
        assert await store.delete_draft(session, record_id) == 6


async def test_compaction_racing_with_writers_loses_nothing(db_engine: AsyncEngine) -> None:
    """Sixty concurrent writers, compaction every 7 updates and explicit compactions in between,
    then a crash: the reloaded draft has every edit, whatever was compacted when."""
    record_id, _ = await create_record(db_engine)
    expected = [f"Edit {index:02d}" for index in range(60)]
    async with manager_for(db_engine, compact_every=7) as manager:
        room = await manager.open(record_id)
        base = room.ydoc.get_update()

        async def writer(content: str) -> None:
            replica = replica_of(room.ydoc)
            await manager.apply_update(record_id, change(replica, add_name(content)), None)

        async def compactor() -> None:
            for _ in range(20):
                await room.compact()
                await asyncio.sleep(0)

        await asyncio.gather(compactor(), *(writer(content) for content in expected))
        assert sorted(names(project(room.ydoc))[1:]) == expected
        assert base  # the hydration itself was never rewritten by a client
        await crash(manager)
    async with manager_for(db_engine) as restarted:
        snapshot = await restarted.snapshot(record_id)
        assert sorted(names(snapshot.jsonld)[1:]) == expected
        assert names(snapshot.jsonld)[0] == "First name"


async def test_a_late_close_after_a_publish_spares_the_draft_opened_since(
    db_engine: AsyncEngine,
) -> None:
    """Publish commits, someone opens the next draft, and only then the publish's close arrives.

    Without ``draft_epoch`` that close discarded the new draft and threw its editors out.
    """
    record_id, _ = await create_record(db_engine, version=1, draft_epoch=0)
    sessions = sessions_of(db_engine)
    async with manager_for(db_engine) as manager:
        old = await manager.open(record_id)
        async with sessions() as session, session.begin():
            await publish(session, record_id, "Version 2")
        fresh = await manager.open(record_id)  # the next editor is quicker than the close
        assert fresh is not old and fresh.draft_epoch == 1
        await manager.apply_update(
            record_id, change(replica_of(fresh.ydoc), add_name("In the new draft")), None
        )

        await manager.close(record_id, discard=True, draft_epoch=0)  # the publish's close
        assert await manager.get(record_id) is fresh and not fresh.closed
        assert await record_epoch(db_engine, record_id) == 1
        row = await draft_row(db_engine, record_id)
        assert row is not None and row.draft_epoch == 1
        assert names((await manager.snapshot(record_id)).jsonld) == [
            "First name",
            "In the new draft",
        ]

        # The same call for the draft that is current does discard it; so does one without.
        await manager.close(record_id, discard=True, draft_epoch=1)
        assert await draft_row(db_engine, record_id) is None
        assert await record_epoch(db_engine, record_id) == 2
        assert fresh.closed
        await manager.open(record_id)
        await manager.close(record_id, discard=True)
        assert await record_epoch(db_engine, record_id) == 3
