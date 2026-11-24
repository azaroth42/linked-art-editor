"""``InMemoryRoomManager`` against PostgreSQL: hydration, persistence order, compaction, epochs."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from pycrdt import Doc, Text, create_update_message
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from lae.crdt import store
from lae.crdt.rooms import (
    CLOSE_EJECTED,
    CLOSE_ROOM_CLOSED,
    CLOSE_STALE_EPOCH,
    Connection,
    DraftExistsError,
    DraftNotFoundError,
    DraftSupersededError,
    InMemoryRoomManager,
    InvalidUpdateError,
    RecordNotEditableError,
    RecordNotFoundError,
    Room,
    RoomClosedError,
    RoomFailedError,
    RoomManager,
)
from lae.db.models import CrdtDraft, CrdtUpdate, Record, RecordStatus
from lae.ids import is_internal_node_iri
from lae.labels.derive import with_derived_label
from lae.projection import ProjectionError, ops, project
from lae.projection.project import record_map
from tests.auth.conftest import create_user
from tests.crdt.helpers import (
    bilingual_record,
    change,
    crash,
    create_record,
    draft_row,
    eventually,
    record_epoch,
    replica_of,
    sample_record,
    sessions_of,
    update_count,
    update_rows,
)


def manager_for(engine: AsyncEngine, **options: Any) -> InMemoryRoomManager:
    return InMemoryRoomManager(sessions_of(engine), **options)


def add_name(content: str) -> Any:
    """An edit adding a ``Name`` with ``content`` to ``identified_by``."""
    return lambda doc: ops.add_node(doc, "identified_by", {"type": "Name", "content": content})


def names(jsonld: dict[str, Any]) -> list[str]:
    return [name["content"] for name in jsonld["identified_by"]]


class FakeChannel:
    """A ``pycrdt.Channel`` fed from a queue; records what the room sends."""

    def __init__(self) -> None:
        self.path = "/fake"
        self.sent: list[bytes] = []
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()

    def __aiter__(self) -> FakeChannel:
        return self

    async def __anext__(self) -> bytes:
        return await self.recv()

    async def send(self, message: bytes) -> None:
        self.sent.append(message)

    async def recv(self) -> bytes:
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message


def connection(user_id: UUID | None = None, *, read_only: bool = False) -> Connection:
    return Connection(
        channel=FakeChannel(),
        user_id=user_id or uuid7(),
        display_name="Tester",
        kind="human",
        read_only=read_only,
    )


def test_in_memory_manager_satisfies_the_protocol() -> None:
    manager: RoomManager = InMemoryRoomManager.__new__(InMemoryRoomManager)
    assert manager is not None


async def test_open_hydrates_once_and_inserts_the_draft_row(db_engine: AsyncEngine) -> None:
    record_id, uri = await create_record(db_engine, version=3, draft_epoch=2)
    user_id = await create_user(db_engine, "Editor")
    async with manager_for(db_engine) as manager:
        assert not await manager.is_open(record_id)
        assert await manager.get(record_id) is None
        room = await manager.open(record_id, created_by=user_id)
        assert await manager.is_open(record_id)
        assert await manager.get(record_id) is room
        assert await manager.open(record_id) is room

        snapshot = await manager.snapshot(record_id)
        assert (snapshot.draft_epoch, snapshot.base_version) == (2, 3)
        assert snapshot.contributors == []
        assert snapshot.last_update_id == 0
        # The projection keeps the internal node IRIs hydration assigned.
        name = snapshot.jsonld["identified_by"][0]
        assert is_internal_node_iri(name["id"], uri)
        expected = sample_record(uri)
        expected["identified_by"][0]["id"] = name["id"]
        expected["referred_to_by"][0]["id"] = snapshot.jsonld["referred_to_by"][0]["id"]
        assert snapshot.jsonld == expected

        row = await draft_row(db_engine, record_id)
        assert row is not None
        assert (row.draft_epoch, row.base_version, row.created_by) == (2, 3, user_id)
        assert row.contributors == []
        stored: Doc[Any] = Doc()
        stored.apply_update(row.state)
        assert project(stored) == snapshot.jsonld
        assert [info.record_id for info in await manager.list_rooms()] == [record_id]


async def test_concurrent_opens_share_one_room_and_one_hydration(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    async with manager_for(db_engine) as manager:
        rooms = await asyncio.gather(*(manager.open(record_id) for _ in range(8)))
        assert len({id(room) for room in rooms}) == 1


async def test_write_is_persisted_and_the_snapshot_reflects_it(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    alice = await create_user(db_engine, "Editor")
    bob = await create_user(db_engine, "Collaborator")
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        before = await draft_row(db_engine, record_id)
        assert before is not None
        replica = replica_of(room.ydoc)

        first = change(replica, add_name("Second name"))
        await manager.apply_update(record_id, first, alice)
        await manager.apply_update(record_id, change(replica, add_name("Third name")), bob)
        await manager.apply_update(record_id, change(replica, add_name("Fourth name")), alice)
        await manager.apply_update(record_id, b"\x00\x00", bob)  # an empty update is not stored

        snapshot = await manager.snapshot(record_id)
        assert names(snapshot.jsonld) == ["First name", "Second name", "Third name", "Fourth name"]
        assert snapshot.jsonld == project(replica)
        assert set(snapshot.contributors) == {alice, bob}

        rows = await update_rows(db_engine, record_id)
        assert len(rows) == 3
        assert rows[0] == first
        after = await draft_row(db_engine, record_id)
        assert after is not None
        assert after.contributors == [alice, bob]  # each user once, in order of first write
        assert after.last_activity_at > before.last_activity_at
        assert after.state == before.state  # nothing compacted yet
        assert snapshot.last_update_id > 0


async def test_restart_rehydrates_from_draft_state_and_update_log(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    user_id = await create_user(db_engine, "Editor")
    first = manager_for(db_engine, compact_every=3)
    await first.start()
    room = await first.open(record_id)
    replica = replica_of(room.ydoc)
    for number in range(5):  # compaction after the third; two stay in crdt_updates
        await first.apply_update(record_id, change(replica, add_name(f"Name {number}")), user_id)
    before = (await first.snapshot(record_id)).jsonld
    assert await update_count(db_engine, record_id) == 2
    await crash(first)  # no clean shutdown: nothing is compacted on the way out

    async with manager_for(db_engine) as second:
        assert not await second.is_open(record_id)
        snapshot = await second.snapshot(record_id)  # loads the room; never creates a draft
        assert snapshot.jsonld == before == project(replica)
        assert snapshot.contributors == [user_id]
        assert (snapshot.draft_epoch, snapshot.base_version) == (0, 1)
        # The rehydrated document is the same Yjs history, so the old replica still merges.
        await second.apply_update(record_id, change(replica, add_name("After restart")), user_id)
        assert (await second.snapshot(record_id)).jsonld == project(replica)
        assert names(project(replica))[-1] == "After restart"


async def test_compaction_every_n_updates_replaces_the_rows(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    async with manager_for(db_engine, compact_every=5) as manager:
        room = await manager.open(record_id)
        replica = replica_of(room.ydoc)
        initial = await draft_row(db_engine, record_id)
        assert initial is not None
        for number in range(12):
            await manager.apply_update(record_id, change(replica, add_name(f"N{number}")), None)
        assert await update_count(db_engine, record_id) == 2  # 12 = 5 + 5 + 2
        row = await draft_row(db_engine, record_id)
        assert row is not None and row.state != initial.state
        assert row.last_activity_at > initial.last_activity_at
        compacted: Doc[Any] = Doc()
        compacted.apply_update(row.state)
        assert len(names(project(compacted))) == 11  # the state holds the first ten
        for payload in await update_rows(db_engine, record_id):
            compacted.apply_update(payload)
        assert project(compacted) == project(replica) == (await manager.snapshot(record_id)).jsonld


async def test_compaction_does_not_count_as_activity(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        await manager.apply_update(record_id, change(replica_of(room.ydoc), add_name("X")), None)
        before = await draft_row(db_engine, record_id)
        await room.compact()
        after = await draft_row(db_engine, record_id)
        assert before is not None and after is not None
        assert after.last_activity_at == before.last_activity_at
        assert after.state != before.state
        assert await update_count(db_engine, record_id) == 0


async def test_compaction_keeps_updates_the_doc_could_not_integrate(
    db_engine: AsyncEngine,
) -> None:
    """Invariant 4: an update that arrived before its dependency survives compaction + restart."""
    record_id, _ = await create_record(db_engine)
    first = manager_for(db_engine, compact_every=2)
    await first.start()
    room = await first.open(record_id)
    replica = replica_of(room.ydoc)
    statement = ops.get_path(replica, "referred_to_by.content")[0]
    assert isinstance(statement, Text)

    def edit(action: Any) -> bytes:
        before = replica.get_state()
        action()
        return replica.get_update(before)

    insert_x = edit(lambda: statement.insert(0, "X"))
    insert_y = edit(lambda: statement.insert(1, "Y"))  # depends on X
    delete_x = edit(lambda: statement.__delitem__(0))  # targets X
    await first.apply_update(record_id, insert_y, None)  # arrives before X
    await first.apply_update(record_id, delete_x, None)  # ... and so does the delete (compacts)
    assert len(room.unintegrated) > 0
    assert await update_count(db_engine, record_id) == 0
    assert project(room.ydoc)["referred_to_by"][0]["content"] == "A statement"
    await crash(first)

    async with manager_for(db_engine, compact_every=2) as second:
        room = await second.open(record_id)
        assert project(room.ydoc)["referred_to_by"][0]["content"] == "A statement"
        await second.apply_update(record_id, insert_x, None)  # the dependency finally arrives
        snapshot = await second.snapshot(record_id)
        assert snapshot.jsonld["referred_to_by"][0]["content"] == "YA statement"
        assert snapshot.jsonld == project(replica)
        await second.apply_update(record_id, change(replica, add_name("Z")), None)  # compacts
        assert room.unintegrated == []
    async with manager_for(db_engine) as third:
        assert (await third.snapshot(record_id)).jsonld == project(replica)


async def test_persist_happens_before_apply_and_broadcast(db_engine: AsyncEngine) -> None:
    """Invariant 2, observed from the hook between the commit and the apply."""
    record_id, _ = await create_record(db_engine)
    seen: dict[str, Any] = {}

    class ObservingRoom(Room):
        async def _persisted(self, update_id: int) -> None:
            seen["rows"] = await update_rows(db_engine, record_id)
            seen["names"] = names(project(self.ydoc))
            seen["sent"] = len(peer.channel.sent)  # type: ignore[attr-defined]

    manager = manager_for(db_engine)
    manager.room_class = ObservingRoom
    async with manager:
        room = await manager.open(record_id)
        peer = connection()
        serving = asyncio.create_task(manager.serve(room, peer))
        await eventually(lambda: len(peer.channel.sent) >= 2, what="sync step 1 + presence")  # type: ignore[attr-defined]
        payload = change(replica_of(room.ydoc), add_name("Durable"))
        await manager.apply_update(record_id, payload, None)
        assert seen["rows"] == [payload], "the row is committed when the hook runs"
        assert seen["names"] == ["First name"], "and the Doc has not changed yet"
        sent_before = seen["sent"]
        await eventually(
            lambda: len(peer.channel.sent) > sent_before,  # type: ignore[attr-defined]
            what="the update is broadcast after the apply",
        )
        assert names(project(room.ydoc)) == ["First name", "Durable"]
        peer.close(CLOSE_ROOM_CLOSED, "test")
        await serving


async def test_an_update_that_cannot_be_persisted_is_not_applied(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_id, _ = await create_record(db_engine)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)

        async def failing(*args: Any, **kwargs: Any) -> int:
            raise OSError("database unreachable")

        monkeypatch.setattr(store, "append_update", failing)
        with pytest.raises(RoomFailedError) as failure:
            await manager.apply_update(
                record_id, change(replica_of(room.ydoc), add_name("Lost")), None
            )
        assert isinstance(failure.value.__cause__, OSError)
        monkeypatch.undo()
        assert names(project(room.ydoc)) == ["First name"]
        assert await update_count(db_engine, record_id) == 0
        assert room.last_update_id == 0
        # The room no longer vouches for its Doc: it is dropped and rebuilt from the rows.
        assert room.closed
        assert await manager.get(record_id) is None
        reopened = await manager.open(record_id)
        assert reopened is not room
        assert names(project(reopened.ydoc)) == ["First name"]


async def test_a_commit_whose_answer_was_lost_is_not_compacted_away(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The insert is committed but the room is told it failed (the connection broke after it).

    The room must not go on with a ``Doc`` that lacks the row: its next compaction would delete
    the row and the edit with it. It is dropped; the reopened room has replayed the row.
    """
    record_id, _ = await create_record(db_engine)
    sessions = sessions_of(db_engine)
    real_append = store.append_update
    async with manager_for(db_engine, compact_every=2) as manager:
        room = await manager.open(record_id)

        async def commits_then_breaks(session: Any, *args: Any) -> int:
            async with sessions() as other, other.begin():
                await real_append(other, *args)
            raise OSError("connection lost after the commit")

        monkeypatch.setattr(store, "append_update", commits_then_breaks)
        with pytest.raises(RoomFailedError):
            await room.write(change(replica_of(room.ydoc), add_name("Committed")), None)
        monkeypatch.undo()
        assert await update_count(db_engine, record_id) == 1
        assert names(project(room.ydoc)) == ["First name"]  # not applied, not broadcast
        with pytest.raises(RoomClosedError):
            await room.write(change(replica_of(room.ydoc), add_name("Too late")), None)

        reopened = await manager.open(record_id)
        assert names(project(reopened.ydoc)) == ["First name", "Committed"]
        await manager.apply_update(
            record_id, change(replica_of(reopened.ydoc), add_name("Next")), None
        )  # the second update of this draft: compaction
        assert await update_count(db_engine, record_id) == 0
    async with manager_for(db_engine) as restarted:
        assert names((await restarted.snapshot(record_id)).jsonld) == [
            "First name",
            "Committed",
            "Next",
        ]


async def test_a_cancelled_caller_does_not_leave_a_write_half_done(
    db_engine: AsyncEngine,
) -> None:
    record_id, _ = await create_record(db_engine)
    reached = asyncio.Event()
    proceed = asyncio.Event()

    class SlowRoom(Room):
        async def _persisted(self, update_id: int) -> None:
            reached.set()
            await proceed.wait()

    manager = manager_for(db_engine)
    manager.room_class = SlowRoom
    async with manager:
        room = await manager.open(record_id)
        payload = change(replica_of(room.ydoc), add_name("Committed"))
        writer = asyncio.create_task(manager.apply_update(record_id, payload, None))
        await reached.wait()  # committed, not yet applied
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        assert names(project(room.ydoc)) == ["First name"]
        proceed.set()
        await eventually(lambda: names(project(room.ydoc)) == ["First name", "Committed"])
        assert await update_rows(db_engine, record_id) == [payload]


async def test_invalid_update_is_rejected_before_anything_is_stored(
    db_engine: AsyncEngine,
) -> None:
    record_id, _ = await create_record(db_engine)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        for garbage in (b"", b"\x01", b"definitely not yjs"):
            with pytest.raises(InvalidUpdateError):
                await manager.apply_update(record_id, garbage, None)
        assert await update_count(db_engine, record_id) == 0
        assert names(project(room.ydoc)) == ["First name"]


async def test_idle_rooms_are_compacted_and_evicted_but_the_draft_persists(
    db_engine: AsyncEngine,
) -> None:
    busy_id, _ = await create_record(db_engine)
    idle_id, _ = await create_record(db_engine)
    user_id = await create_user(db_engine, "Editor")
    async with manager_for(db_engine, idle_seconds=0.2) as manager:
        busy = await manager.open(busy_id)
        idle = await manager.open(idle_id)
        replica = replica_of(idle.ydoc)
        await manager.apply_update(idle_id, change(replica, add_name("Kept")), user_id)
        assert await manager.evict_idle() == []  # the write counts as activity

        peer = connection()
        serving = asyncio.create_task(manager.serve(busy, peer))
        await eventually(lambda: peer in busy.connections)
        await asyncio.sleep(0.3)
        assert await manager.evict_idle() == [idle_id]  # a room with a socket is never evicted
        assert await manager.is_open(busy_id) and not await manager.is_open(idle_id)

        assert await update_count(db_engine, idle_id) == 0  # compacted on the way out
        row = await draft_row(db_engine, idle_id)
        assert row is not None and row.contributors == [user_id]
        reopened = await manager.open(idle_id)
        assert reopened is not idle
        assert project(reopened.ydoc) == project(replica)

        peer.close(CLOSE_ROOM_CLOSED, "test")
        await serving
        await asyncio.sleep(0.3)  # idle since the socket left
        assert busy_id in await manager.evict_idle()


async def test_the_sweeper_evicts_without_being_asked(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    async with manager_for(db_engine, idle_seconds=0.05, sweep_seconds=0.05) as manager:
        await manager.open(record_id)
        await eventually(lambda: record_id not in manager._rooms, what="swept")
        assert await draft_row(db_engine, record_id) is not None


async def test_close_without_discard_compacts_and_keeps_the_draft(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        replica = replica_of(room.ydoc)
        await manager.apply_update(record_id, change(replica, add_name("Kept")), None)
        peer = connection()
        serving = asyncio.create_task(manager.serve(room, peer))
        await eventually(lambda: peer in room.connections)

        await manager.close(record_id, discard=False)
        await serving
        assert (peer.close_code, peer.close_reason) == (CLOSE_ROOM_CLOSED, "room-closed")
        assert not await manager.is_open(record_id)
        assert await update_count(db_engine, record_id) == 0
        assert await record_epoch(db_engine, record_id) == 0
        assert (await manager.snapshot(record_id)).jsonld == project(replica)
        await manager.close(uuid7(), discard=False)  # unknown record: nothing to do


async def test_discard_deletes_the_draft_closes_sockets_and_ends_the_epoch(
    db_engine: AsyncEngine,
) -> None:
    record_id, _ = await create_record(db_engine, draft_epoch=4)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        replica = replica_of(room.ydoc)
        await manager.apply_update(record_id, change(replica, add_name("Discarded")), None)
        peer = connection()
        serving = asyncio.create_task(manager.serve(room, peer))
        await eventually(lambda: peer in room.connections)

        await manager.close(record_id, discard=True)
        await serving
        assert (peer.close_code, peer.close_reason) == (CLOSE_STALE_EPOCH, "stale-epoch")
        assert await draft_row(db_engine, record_id) is None
        assert await update_count(db_engine, record_id) == 0
        assert await record_epoch(db_engine, record_id) == 5

        with pytest.raises(DraftNotFoundError):
            await manager.snapshot(record_id)
        assert await draft_row(db_engine, record_id) is None  # snapshot never creates a draft

        fresh = await manager.open(record_id)
        assert fresh.draft_epoch == 5
        assert names(project(fresh.ydoc)) == ["First name"]
        await manager.close(record_id, discard=True)
        assert await record_epoch(db_engine, record_id) == 6
        await manager.close(record_id, discard=True)  # no draft: the epoch stays
        assert await record_epoch(db_engine, record_id) == 6
        await manager.close(uuid7(), discard=True)  # unknown record: nothing to do


async def publish_behind_the_room(engine: AsyncEngine, record_id: UUID) -> None:
    """What the publish transaction (Step 1.10) does to the draft: bump the epoch, delete it."""
    async with AsyncSession(engine) as session:
        await session.execute(
            update(Record)
            .where(Record.id == record_id)
            .values(draft_epoch=Record.draft_epoch + 1, version=Record.version + 1)
        )
        await store.delete_draft(session, record_id)
        await session.commit()


async def test_close_after_a_publish_does_not_bump_the_epoch_again(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine, version=1, draft_epoch=0)
    async with manager_for(db_engine) as manager:
        await manager.open(record_id)
        await publish_behind_the_room(db_engine, record_id)
        assert await record_epoch(db_engine, record_id) == 1
        await manager.close(record_id, discard=True)
        assert await record_epoch(db_engine, record_id) == 1
        assert not await manager.is_open(record_id)
        room = await manager.open(record_id)
        assert (room.draft_epoch, room.base_version) == (1, 2)


async def test_a_room_whose_draft_was_published_writes_nothing(db_engine: AsyncEngine) -> None:
    """Invariant 3: the publish's best-effort ``close`` never arrived."""
    record_id, _ = await create_record(db_engine)
    async with manager_for(db_engine, compact_every=1) as manager:
        stale = await manager.open(record_id)
        replica = replica_of(stale.ydoc)
        peer = connection()
        serving = asyncio.create_task(manager.serve(stale, peer))
        await eventually(lambda: peer in stale.connections)
        await publish_behind_the_room(db_engine, record_id)

        with pytest.raises(DraftSupersededError):
            await stale.write(change(replica, add_name("Too late")), None)
        assert await update_count(db_engine, record_id) == 0
        assert await draft_row(db_engine, record_id) is None  # nothing was resurrected
        assert stale.closed

        # Every entry point notices: open() retires the stale room and hydrates the new draft.
        fresh = await manager.open(record_id)
        await serving
        assert (peer.close_code, peer.close_reason) == (CLOSE_STALE_EPOCH, "stale-epoch")
        assert fresh is not stale and fresh.draft_epoch == 1
        assert names(project(fresh.ydoc)) == ["First name"]


async def test_open_notices_a_published_draft_without_a_write(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    async with manager_for(db_engine) as manager:
        stale = await manager.open(record_id)
        await publish_behind_the_room(db_engine, record_id)
        with pytest.raises(DraftNotFoundError):
            await manager.snapshot(record_id)  # not the stale room's content
        assert stale.closed and not await manager.is_open(record_id)


async def test_a_socket_write_into_a_superseded_room_closes_everyone_with_4409(
    db_engine: AsyncEngine,
) -> None:
    record_id, _ = await create_record(db_engine)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        writer, reader = connection(), connection()
        tasks = [
            asyncio.create_task(manager.serve(room, writer)),
            asyncio.create_task(manager.serve(room, reader)),
        ]
        await eventually(lambda: len(room.connections) == 2)
        await publish_behind_the_room(db_engine, record_id)
        payload = change(replica_of(room.ydoc), add_name("Too late"))
        writer.channel.incoming.put_nowait(create_update_message(payload))  # type: ignore[attr-defined]
        await asyncio.gather(*tasks)
        assert writer.close_code == reader.close_code == CLOSE_STALE_EPOCH
        assert not await manager.is_open(record_id)
        assert await update_count(db_engine, record_id) == 0


async def test_stale_draft_row_is_replaced_by_a_fresh_hydration(db_engine: AsyncEngine) -> None:
    """A draft row left at an old epoch (it should not exist) is never replayed."""
    record_id, _ = await create_record(db_engine, draft_epoch=0)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        await manager.apply_update(record_id, change(replica_of(room.ydoc), add_name("Old")), None)
        await crash(manager)
    async with AsyncSession(db_engine) as session:
        await session.execute(update(Record).where(Record.id == record_id).values(draft_epoch=3))
        await session.commit()
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        assert room.draft_epoch == 3
        assert names(project(room.ydoc)) == ["First name"]
        assert await update_count(db_engine, record_id) == 0


async def test_open_with_content_hydrates_a_new_draft_from_it(db_engine: AsyncEngine) -> None:
    record_id, uri = await create_record(db_engine)
    historical = sample_record(uri)
    historical["identified_by"][0]["content"] = "Historical name"
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id, content=historical)
        assert names(project(room.ydoc)) == ["Historical name"]
        with pytest.raises(DraftExistsError):
            await manager.open(record_id, content=historical)
        await manager.close(record_id, discard=False)
        with pytest.raises(DraftExistsError):  # the draft exists even when the room is not loaded
            await manager.open(record_id, content=historical)
        # Revert (Step 1.14): discard, then open with the revision's content.
        await manager.close(record_id, discard=True)
        room = await manager.open(record_id, content=sample_record(uri))
        assert names(project(room.ydoc)) == ["First name"] and room.draft_epoch == 1


async def test_open_errors(db_engine: AsyncEngine) -> None:
    deleted_id, _ = await create_record(db_engine, status=RecordStatus.DELETED)
    record_id, uri = await create_record(db_engine)
    async with manager_for(db_engine) as manager:
        with pytest.raises(RecordNotFoundError):
            await manager.open(uuid7())
        with pytest.raises(RecordNotFoundError):
            await manager.snapshot(uuid7())
        with pytest.raises(RecordNotEditableError):
            await manager.open(deleted_id)
        with pytest.raises(DraftNotFoundError):
            await manager.open(record_id, create=False)
        with pytest.raises(ProjectionError):
            await manager.open(record_id, content={"id": uri, "type": "Person", "_order": 1})
        assert await draft_row(db_engine, record_id) is None
        assert await draft_row(db_engine, deleted_id) is None
        assert await manager.list_rooms() == []


async def test_eject_closes_only_that_users_sockets(db_engine: AsyncEngine) -> None:
    first_id, _ = await create_record(db_engine)
    second_id, _ = await create_record(db_engine)
    suspended, other = uuid7(), uuid7()
    async with manager_for(db_engine) as manager:
        first, second = await manager.open(first_id), await manager.open(second_id)
        targets = [connection(suspended), connection(suspended)]
        bystander = connection(other)
        tasks = [
            asyncio.create_task(manager.serve(first, targets[0])),
            asyncio.create_task(manager.serve(second, targets[1])),
            asyncio.create_task(manager.serve(first, bystander)),
        ]
        await eventually(lambda: len(first.connections) == 2 and len(second.connections) == 1)
        assert await manager.eject(suspended) == 2
        await asyncio.gather(*tasks[:2])
        assert [c.close_code for c in targets] == [CLOSE_EJECTED, CLOSE_EJECTED]
        assert bystander.close_code is None and bystander in first.connections
        assert await manager.eject(suspended) == 0
        bystander.close(CLOSE_ROOM_CLOSED, "test")
        await tasks[2]


async def test_read_only_connection_updates_are_dropped(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        viewer = connection(read_only=True)
        serving = asyncio.create_task(manager.serve(room, viewer))
        await eventually(lambda: viewer in room.connections)
        payload = change(replica_of(room.ydoc), add_name("Not allowed"))
        step2 = bytes([0, 1]) + create_update_message(payload)[2:]
        for frame in (step2, create_update_message(payload)):
            viewer.channel.incoming.put_nowait(frame)  # type: ignore[attr-defined]
        await eventually(lambda: viewer.dropped_updates == 2)
        assert names(project(room.ydoc)) == ["First name"]
        assert await update_count(db_engine, record_id) == 0
        assert room.awareness.get_local_state() == {
            "kind": "server",
            "draft_epoch": 0,
            "base_version": 1,
            "read_only": [str(viewer.user_id)],
        }
        viewer.channel.incoming.put_nowait(None)  # type: ignore[attr-defined]
        await serving
        assert room.awareness.get_local_state()["read_only"] == []  # type: ignore[index]


async def test_garbage_frames_close_only_that_socket(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        for frame in (
            b"\x00\x02\x05ab",
            b"\x00\x02\x03abc",
            b"\x00\x09\x00",
            b"\x01\x05{",
            b"\x00",
        ):
            bad, good = connection(), connection()
            tasks = [
                asyncio.create_task(manager.serve(room, bad)),
                asyncio.create_task(manager.serve(room, good)),
            ]
            await eventually(lambda: len(room.connections) == 2)
            bad.channel.incoming.put_nowait(frame)  # type: ignore[attr-defined]
            await tasks[0]
            assert (bad.close_code, bad.close_reason) == (4400, "protocol-error"), frame
            assert good.close_code is None
            good.close(CLOSE_ROOM_CLOSED, "test")
            await tasks[1]
        assert await update_count(db_engine, record_id) == 0
        assert await manager.is_open(record_id)


async def test_transact_and_revert_are_declared_for_phase_3(db_engine: AsyncEngine) -> None:
    manager = manager_for(db_engine)
    with pytest.raises(NotImplementedError):
        await manager.transact(uuid7(), None, lambda doc: None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        await manager.revert(uuid7(), [])


async def test_stop_compacts_every_room(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        replica = replica_of(room.ydoc)
        await manager.apply_update(record_id, change(replica, add_name("Kept")), None)
    assert await update_count(db_engine, record_id) == 0
    row = await draft_row(db_engine, record_id)
    assert row is not None
    restored: Doc[Any] = Doc()
    restored.apply_update(row.state)
    assert project(restored) == project(replica)


async def test_store_guards_match_only_the_current_epoch(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    sessions = sessions_of(db_engine)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        payload = change(replica_of(room.ydoc), add_name("X"))
    async with sessions() as session, session.begin():
        assert await store.append_update(session, record_id, 7, payload, None) is None
        assert not await store.compact(session, record_id, 7, b"state", 10**9)
        assert await store.append_update(session, uuid7(), 0, payload, None) is None
    assert await update_count(db_engine, record_id) == 0
    async with sessions() as session, session.begin():
        assert await store.append_update(session, record_id, 0, payload, None) is not None
        assert await store.draft_is_current(session, record_id, 0)
        assert not await store.draft_is_current(session, record_id, 1)
    assert await update_count(db_engine, record_id) == 1


async def test_delete_draft_leaves_updated_at_alone(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    sessions = sessions_of(db_engine)
    async with sessions() as session:
        before = await session.scalar(select(Record.updated_at).where(Record.id == record_id))
    async with manager_for(db_engine) as manager:
        await manager.open(record_id)
        await manager.close(record_id, discard=True)
    async with sessions() as session:
        after = await session.scalar(select(Record.updated_at).where(Record.id == record_id))
        assert after == before
        assert await store.delete_draft(session, uuid7()) is None
        leftovers = await session.scalar(
            select(CrdtDraft.record_id).where(CrdtDraft.record_id == record_id)
        )
        assert leftovers is None
        await session.execute(delete(CrdtUpdate).where(CrdtUpdate.record_id == record_id))


async def test_record_map_is_the_only_root(db_engine: AsyncEngine) -> None:
    """The draft state stores exactly what ``lae.projection.hydrate`` builds (one root map)."""
    record_id, uri = await create_record(db_engine)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        assert record_map(room.ydoc)["id"] == uri


# --- the derived root _label (Step 1.19a, contract 1.19a §5 tests 11-13a) ----------------------


def retitle(index: int, insert: str, at: int = 0) -> Any:
    """An edit inserting ``insert`` at ``at`` into the content of the ``index``-th Name."""

    def edit(doc: Doc[Any]) -> None:
        entry = ops.list_entries(doc, "identified_by")[index][1]
        ops.splice_text(doc, "content", at, 0, insert, base=entry)

    return edit


async def test_snapshot_carries_the_derived_label(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine, data=bilingual_record)
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        # Hydrated with a stale _label: the snapshot has the English primary name instead.
        assert record_map(room.ydoc)["_label"] == "Stale label"
        snapshot = await manager.snapshot(record_id)
        assert snapshot.jsonld["_label"] == "The Night Watch"
        assert snapshot.jsonld == with_derived_label(project(room.ydoc), "en")

        # It follows an edit of the name made through a replica, as a socket's update is.
        replica = replica_of(room.ydoc)
        await manager.apply_update(record_id, change(replica, retitle(0, "  Corporal  ")), None)
        snapshot = await manager.snapshot(record_id)
        assert snapshot.jsonld["identified_by"][0]["content"] == "  Corporal  The Night Watch"
        assert snapshot.jsonld["_label"] == "Corporal The Night Watch"
        assert snapshot.jsonld == with_derived_label(project(replica), "en")
        # The Doc itself still holds the stale label: nothing was written for it.
        assert record_map(room.ydoc)["_label"] == "Stale label"

        # Without a usable name the document's own label is kept.
        def blank_every_name(doc: Doc[Any]) -> None:
            for _, entry in ops.list_entries(doc, "identified_by"):
                ops.set_scalar(doc, "content", " ", base=entry)

        await manager.apply_update(record_id, change(replica, blank_every_name), None)
        assert (await manager.snapshot(record_id)).jsonld["_label"] == "Stale label"


async def test_deriving_writes_nothing(db_engine: AsyncEngine) -> None:
    """Invariant 6: no crdt_updates row, no last_update_id, no Doc transaction for the label."""
    record_id, _ = await create_record(db_engine, data=bilingual_record)
    user_id = await create_user(db_engine, "Editor")
    async with manager_for(db_engine) as manager:
        room = await manager.open(record_id)
        transactions: list[Any] = []
        subscription = room.ydoc.observe(transactions.append)
        replica = replica_of(room.ydoc)
        for number in range(3):
            state = room.ydoc.get_state()
            last_update_id = room.last_update_id
            rows = await update_count(db_engine, record_id)
            row = await draft_row(db_engine, record_id)
            seen = len(transactions)
            for _ in range(3):
                assert (await manager.snapshot(record_id)).jsonld["_label"]
                assert room.snapshot().jsonld["_label"]
            assert room.ydoc.get_state() == state
            assert room.last_update_id == last_update_id
            assert await update_count(db_engine, record_id) == rows == number
            after = await draft_row(db_engine, record_id)
            assert row is not None and after is not None
            assert (after.state, after.contributors, after.last_update_id) == (
                row.state,
                row.contributors,
                row.last_update_id,
            )
            assert len(transactions) == seen
            # One client update: exactly one row appended, one transaction.
            await manager.apply_update(record_id, change(replica, retitle(0, "x")), user_id)
            assert await update_count(db_engine, record_id) == number + 1
            assert len(transactions) == seen + 1
        room.ydoc.unobserve(subscription)


async def test_concurrent_name_edits_give_one_label_in_either_order(
    db_engine: AsyncEngine,
) -> None:
    labels = []
    for first_is_a in (True, False):
        record_id, _ = await create_record(db_engine, data=bilingual_record)
        async with manager_for(db_engine) as manager:
            room = await manager.open(record_id)
            a, b = replica_of(room.ydoc), replica_of(room.ydoc)
            from_a = change(a, retitle(0, " (A)", at=len("The Night Watch")))
            from_b = change(b, retitle(0, "B: "))
            order = (from_a, from_b) if first_is_a else (from_b, from_a)
            for update in order:
                await manager.apply_update(record_id, update, None)
            a.apply_update(from_b)
            b.apply_update(from_a)
            snapshot = await manager.snapshot(record_id)
            for replica in (a, b):
                assert snapshot.jsonld == with_derived_label(project(replica), "en")
            labels.append(snapshot.jsonld["_label"])
    assert labels == ["B: The Night Watch (A)", "B: The Night Watch (A)"]


async def test_a_room_derives_in_its_managers_label_language(db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine, data=bilingual_record)
    async with manager_for(db_engine, label_language="fr") as manager:
        room = await manager.open(record_id)
        assert room.label_language == "fr"
        assert (await manager.snapshot(record_id)).jsonld["_label"] == "La Ronde de nuit"
        await manager.close(record_id, discard=False)
        # A room rebuilt from the rows gets the manager's language too.
        assert (await manager.snapshot(record_id)).jsonld["_label"] == "La Ronde de nuit"
    async with manager_for(db_engine) as manager:
        assert (await manager.snapshot(record_id)).jsonld["_label"] == "The Night Watch"
