"""Decision A4 enforced: one ``crdt-ws`` per database (``lae.crdt.instance``)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.crdt.instance import INSTANCE_LOCK_KEY, InstanceLock, InstanceLockError
from lae.crdt.rooms import InMemoryRoomManager, RoomFailedError
from lae.crdt.server import create_crdt_app
from lae.projection import ops, project
from tests.crdt.helpers import (
    change,
    crdt_settings,
    create_record,
    draft_row,
    eventually,
    replica_of,
    sessions_of,
    update_count,
)


def add_name(content: str) -> Any:
    return lambda doc: ops.add_node(doc, "identified_by", {"type": "Name", "content": content})


def names(jsonld: dict[str, Any]) -> list[str]:
    return [name["content"] for name in jsonld["identified_by"]]


async def nothing() -> None:
    """An ``on_lost`` callback for locks that are not expected to be lost."""


async def lock_holders(engine: AsyncEngine) -> list[int]:
    """Backend pids holding the instance lock in this database."""
    async with AsyncSession(engine) as session:
        rows = await session.execute(
            text(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "AND ((classid::bigint << 32) | objid::bigint) = :key"
            ),
            {"key": INSTANCE_LOCK_KEY},
        )
        return [row[0] for row in rows]


async def test_why_two_managers_on_one_database_lose_an_edit(db_engine: AsyncEngine) -> None:
    """What the lock prevents. Two managers hold the same draft; A compacts; B's edit is gone."""
    record_id, _ = await create_record(db_engine)
    sessions = sessions_of(db_engine)
    a = InMemoryRoomManager(sessions, compact_every=2)
    b = InMemoryRoomManager(sessions, compact_every=1000)
    room_a, room_b = await a.open(record_id), await b.open(record_id)
    await room_b.write(change(replica_of(room_b.ydoc), add_name("From B")), None)
    await room_a.write(change(replica_of(room_a.ydoc), add_name("From A 1")), None)
    await room_a.write(change(replica_of(room_a.ydoc), add_name("From A 2")), None)  # compacts
    assert await update_count(db_engine, record_id) == 0  # B's row was deleted with A's
    await a.stop()
    await b.abandon()
    async with InMemoryRoomManager(sessions) as restarted:
        survived = names((await restarted.snapshot(record_id)).jsonld)
    assert survived == ["First name", "From A 1", "From A 2"], "B's persisted edit is lost"


async def test_a_second_holder_is_refused_until_the_first_lets_go(db_engine: AsyncEngine) -> None:
    first = InstanceLock(db_engine, on_lost=nothing)
    second = InstanceLock(db_engine, on_lost=nothing, acquire_seconds=0.6)
    await first.acquire()
    try:
        assert first.held and len(await lock_holders(db_engine)) == 1
        with pytest.raises(InstanceLockError, match="another crdt-ws"):
            await second.acquire()
        assert not second.held
        # A successor that starts while the predecessor is shutting down waits for it.
        patient = InstanceLock(db_engine, on_lost=nothing, acquire_seconds=10)
        waiting = asyncio.create_task(patient.acquire())
        await asyncio.sleep(0.3)
        assert not waiting.done()
        await first.release()
        await asyncio.wait_for(waiting, 5)
        assert patient.held and not first.held
        await patient.release()
    finally:
        await first.release()
    assert await lock_holders(db_engine) == []
    await first.release()  # releasing twice is harmless


async def test_losing_the_connection_loses_the_lock_and_calls_on_lost(
    db_engine: AsyncEngine,
) -> None:
    lost = asyncio.Event()

    async def on_lost() -> None:
        lost.set()

    lock = InstanceLock(db_engine, on_lost=on_lost, check_seconds=0.1)
    await lock.acquire()
    try:
        (pid,) = await lock_holders(db_engine)
        async with AsyncSession(db_engine) as session:  # a database restart, for this session
            await session.execute(select(func.pg_terminate_backend(pid)))
        await asyncio.wait_for(lost.wait(), 5)
        assert not lock.held
        assert await lock_holders(db_engine) == []
    finally:
        await lock.release()


async def test_a_second_crdt_ws_on_the_database_does_not_start(
    db_url: str, db_engine: AsyncEngine
) -> None:
    first = create_crdt_app(crdt_settings(db_url))
    second = create_crdt_app(crdt_settings(db_url), instance_lock_seconds=0.6)
    async with first.router.lifespan_context(first):
        with pytest.raises(InstanceLockError):
            async with second.router.lifespan_context(second):
                pytest.fail("a second crdt-ws started on the same database")
        assert first.state.instance_lock.held
    assert await lock_holders(db_engine) == []
    async with second.router.lifespan_context(second):  # the first is gone: now it may
        assert second.state.instance_lock.held


async def test_a_process_that_lost_the_lock_drops_its_rooms_without_writing(
    db_url: str, db_engine: AsyncEngine
) -> None:
    record_id, _ = await create_record(db_engine)
    terminated: list[bool] = []
    app = create_crdt_app(crdt_settings(db_url), terminate=lambda: terminated.append(True))
    async with app.router.lifespan_context(app):
        manager: InMemoryRoomManager = app.state.rooms
        lock: InstanceLock = app.state.instance_lock
        lock._check_seconds = 0.1
        room = await manager.open(record_id)
        await manager.apply_update(
            record_id, change(replica_of(room.ydoc), add_name("Persisted")), None
        )
        state_before = (await draft_row(db_engine, record_id)).state  # type: ignore[union-attr]
        (pid,) = await lock_holders(db_engine)
        async with AsyncSession(db_engine) as session:
            await session.execute(select(func.pg_terminate_backend(pid)))
        await eventually(lambda: bool(terminated), what="the process asks to be stopped")
        assert room.closed and not await manager.is_open(record_id)
        with pytest.raises(RoomFailedError):
            await manager.open(record_id)
    # Nothing was compacted over the database: the row is still the hydration, the update is
    # still in the log, and a new process serves both.
    row = await draft_row(db_engine, record_id)
    assert row is not None and row.state == state_before
    assert await update_count(db_engine, record_id) == 1
    async with InMemoryRoomManager(sessions_of(db_engine)) as successor:
        reloaded = await successor.open(record_id)
        assert names(project(reloaded.ydoc)) == ["First name", "Persisted"]
