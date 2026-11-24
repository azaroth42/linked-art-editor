"""Publish under concurrency and failure (Step 1.10): the integrity boundary.

Three things must hold whatever the timing:

- two publishes of one draft produce exactly one version;
- a publish that races an edit either contains the edit or fails without writing anything, and
  the edit is never silently dropped;
- a failure anywhere before the commit leaves every table, and the draft, as they were.

The interleavings are forced, not hoped for: a test holds a row lock, or parks the publish at a
known point, and looks at ``pg_stat_activity`` until the other side is really waiting.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.api.records import new_record_data
from lae.crdt import store
from lae.crdt.rooms import DraftSupersededError
from lae.db.models import QaFindingStatus, Record
from lae.projection import ops
from lae.publish import errors, service
from tests.crdt.helpers import crash, eventually
from tests.publish.conftest import (
    NO_PRIMARY_NAME,
    Caller,
    Network,
    Served,
    Stored,
    add_finding,
    caller,
    edit,
    new_draft,
    open_draft,
    painting,
    person,
    publish,
    stored,
)

WAITING_FOR_A_LOCK = text(
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
)


def add_name(content: str) -> Any:
    return lambda doc: ops.add_node(doc, "identified_by", {"type": "Name", "content": content})


def names(document: dict[str, Any]) -> list[str]:
    return [name["content"] for name in document["identified_by"]]


async def lock_waiters(engine: AsyncEngine) -> int:
    """Number of backends of the test database that are waiting for a lock."""
    async with AsyncSession(engine) as session:
        return int(await session.scalar(WAITING_FOR_A_LOCK) or 0)


def waiting(engine: AsyncEngine, count: int) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return await lock_waiters(engine) >= count

    return check


def fingerprint(state: Stored) -> tuple[Any, ...]:
    """Everything a failed publish must leave unchanged."""
    record = state.record
    assert record is not None
    draft = state.draft
    label = state.label
    return (
        state.counts,
        record.version,
        record.draft_epoch,
        record.etag,
        record.data,
        record.updated_at,
        None if draft is None else (draft.draft_epoch, draft.last_update_id, bytes(draft.state)),
        None
        if label is None
        else (label.type, label.labels, label.default_label, label.metatype_uris, label.updated_at),
        [
            (finding.code, finding.status, finding.resolved_by, finding.updated_at)
            for finding in state.findings
        ],
    )


def outcome(response: httpx.Response) -> tuple[int, str | None]:
    return response.status_code, response.json().get("code")


# -- double publish -----------------------------------------------------------------------------


async def test_two_publishes_of_one_draft_make_exactly_one_version(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    """Both validate the same snapshot and reach the row lock; one commits, the other is told."""
    editor = await caller(db_engine)
    second_editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person())
    await edit(network, record_id, editor.id, add_name("Second name"))

    async with db_engine.connect() as blocker:
        await blocker.execute(select(Record.id).where(Record.id == record_id).with_for_update())
        racing = [
            asyncio.create_task(publish(served, who, record_id, summary=summary))
            for who, summary in ((editor, "one"), (second_editor, "two"))
        ]
        await eventually(waiting(db_engine, 2), what="both publishes wait for the row lock")
        await blocker.rollback()
    responses = await asyncio.gather(*racing)

    assert sorted(outcome(response) for response in responses) == [
        (200, None),
        (409, errors.DRAFT_CHANGED),
    ]
    lost = next(response for response in responses if response.status_code == 409)
    assert lost.json()["params"] == {"id": str(record_id)}
    after = await stored(db_engine, record_id)
    assert after.counts == (1, 1, 1, 0, 0)
    assert after.record is not None
    assert (after.record.version, after.record.draft_epoch) == (1, 1)
    assert names(after.record.data) == ["Ada Lovelace", "Second name"]


async def test_many_simultaneous_publishes_make_exactly_one_version(
    served: Served, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person())

    responses = await asyncio.gather(*(publish(served, editor, record_id) for _ in range(8)))

    outcomes = [outcome(response) for response in responses]
    assert outcomes.count((200, None)) == 1, outcomes
    # A loser either lost the row lock or arrived after the draft was gone.
    assert all(
        result in ((200, None), (409, errors.DRAFT_CHANGED), (409, errors.NO_DRAFT))
        for result in outcomes
    ), outcomes
    after = await stored(db_engine, record_id)
    assert after.counts == (1, 1, 1, 0, 0)
    assert after.record is not None and after.record.version == 1


async def test_bundles_that_share_stubs_lock_them_in_one_order(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    """Two bundles reach the same two stubs from opposite ends and must not deadlock.

    The stubs refer to each other, so one publish discovers them as (first, second) and the
    other as (second, first). Locked in discovery order, each publish would hold one stub and
    wait for the other; locked in UUID order, both queue for the same row.
    """
    editor = await caller(db_engine)
    first_id, first_uri = await new_draft(served, editor, "Group", person("First workshop"))
    second_id, second_uri = await new_draft(
        served,
        editor,
        "Group",
        {**person("Second workshop"), "member_of": [{"id": first_uri, "type": "Group"}]},
    )
    await network.rooms.close(first_id, discard=True)
    await network.rooms.open(
        first_id,
        content=new_record_data(
            "Group",
            first_uri,
            1,
            {**person("First workshop"), "member_of": [{"id": second_uri, "type": "Group"}]},
            label_language="en",
        ),
    )
    one_id, _ = await new_draft(served, editor, "HumanMadeObject", painting("One", first_uri))
    two_id, _ = await new_draft(served, editor, "HumanMadeObject", painting("Two", second_uri))
    stubs = sorted([first_id, second_id])

    async with db_engine.connect() as blocker:
        await blocker.execute(select(Record.id).where(Record.id.in_(stubs)).with_for_update())
        racing = [
            asyncio.create_task(publish(served, editor, object_id, NO_PRIMARY_NAME))
            for object_id in (one_id, two_id)
        ]
        await eventually(waiting(db_engine, 2), what="both publishes wait for a stub")
        await blocker.rollback()
    responses = await asyncio.wait_for(asyncio.gather(*racing), timeout=20)

    assert sorted(outcome(response) for response in responses) == [
        (200, None),
        (409, errors.DRAFT_CHANGED),
    ]
    for object_id, response in zip((one_id, two_id), responses, strict=True):
        if response.status_code == 200:
            assert response.json()["published"] == [str(object_id), *map(str, stubs)]
        else:
            # The first stub in lock order is where the loser finds its snapshot outdated.
            assert response.json()["params"] == {"id": str(stubs[0])}
            assert (await stored(db_engine, object_id)).counts == (0, 0, 0, 1, 0)
            # The stubs are published now; the retry is an ordinary publish of one record.
            retry = await publish(served, editor, object_id, NO_PRIMARY_NAME)
            assert retry.json()["published"] == [str(object_id)]
        assert (await stored(db_engine, object_id)).counts == (1, 1, 1, 0, 0)
    for stub_id in stubs:
        stub = await stored(db_engine, stub_id)
        assert stub.counts == (1, 1, 1, 0, 0)
        assert stub.record is not None and stub.record.version == 1


# -- a publish racing an edit -------------------------------------------------------------------


def edit_after_validation(
    monkeypatch: pytest.MonkeyPatch, network: Network, record_id: UUID, who: Caller, content: str
) -> None:
    """Make the next publish see one more edit of ``record_id`` right after it has validated."""
    validate = service._validate

    async def validate_then_edit(*args: Any, **kwargs: Any) -> None:
        await validate(*args, **kwargs)
        await edit(network, record_id, who.id, add_name(content))

    monkeypatch.setattr(service, "_validate", validate_then_edit)


async def test_an_edit_after_the_snapshot_fails_the_publish_and_stays_in_the_draft(
    served: Served, network: Network, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor = await caller(db_engine)
    typist = await caller(db_engine, "Collaborator")
    record_id, _ = await new_draft(served, editor, "Person", person("Ada"))

    with monkeypatch.context() as patched:
        edit_after_validation(patched, network, record_id, typist, "Typed during the publish")
        response = await publish(served, editor, record_id)

    assert outcome(response) == (409, errors.DRAFT_CHANGED)
    assert response.json()["params"] == {"id": str(record_id)}
    after = await stored(db_engine, record_id)
    assert after.counts == (0, 0, 0, 1, 1)
    assert after.record is not None
    assert (after.record.version, after.record.draft_epoch) == (0, 0)
    room = await network.rooms.get(record_id)
    assert room is not None and room.draft_epoch == 0  # nobody was disconnected
    assert names((await network.rooms.snapshot(record_id)).jsonld) == [
        "Ada",
        "Typed during the publish",
    ]

    # Published again, the edit is part of the version, credited to who made it.
    assert (await publish(served, editor, record_id)).status_code == 200
    final = await stored(db_engine, record_id)
    assert final.record is not None
    assert names(final.record.data) == ["Ada", "Typed during the publish"]
    assert final.revision(1).authors == sorted([editor.id, typist.id])


async def test_an_edit_of_a_bundled_stub_fails_the_publish_of_the_whole_bundle(
    served: Served, network: Network, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor = await caller(db_engine)
    maker_id, maker_uri = await new_draft(served, editor, "Person", person("Ada"))
    object_id, _ = await new_draft(served, editor, "HumanMadeObject", painting("Engine", maker_uri))

    with monkeypatch.context() as patched:
        edit_after_validation(patched, network, maker_id, editor, "Typed during the publish")
        response = await publish(served, editor, object_id, NO_PRIMARY_NAME)

    assert outcome(response) == (409, errors.DRAFT_CHANGED)
    assert response.json()["params"] == {"id": str(maker_id)}
    assert (await stored(db_engine, object_id)).counts == (0, 0, 0, 1, 0)
    assert (await stored(db_engine, maker_id)).counts == (0, 0, 0, 1, 1)
    assert names((await network.rooms.snapshot(maker_id)).jsonld) == [
        "Ada",
        "Typed during the publish",
    ]


async def test_an_edit_that_arrives_during_the_transaction_waits_and_is_refused(
    served: Served, network: Network, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other order: the publish holds its locks first. The update must not slip in."""
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person("Ada"))
    in_transaction, release = asyncio.Event(), asyncio.Event()
    upsert = service.upsert_entity_labels_for_record

    async def parked_upsert(*args: Any, **kwargs: Any) -> None:
        in_transaction.set()
        await release.wait()
        await upsert(*args, **kwargs)

    monkeypatch.setattr(service, "upsert_entity_labels_for_record", parked_upsert)
    publishing = asyncio.create_task(publish(served, editor, record_id))
    await asyncio.wait_for(in_transaction.wait(), 10)
    late = asyncio.create_task(edit(network, record_id, editor.id, add_name("Too late")))
    await eventually(waiting(db_engine, 1), what="the update waits for the publish")
    assert not late.done()
    release.set()

    response = await publishing
    assert response.status_code == 200, response.text
    with pytest.raises(DraftSupersededError):
        await late
    after = await stored(db_engine, record_id)
    assert after.counts == (1, 1, 1, 0, 0)
    assert after.record is not None and names(after.record.data) == ["Ada"]
    # The next draft is a new hydration of the published version, at the next epoch.
    assert await open_draft(network, record_id) == 1
    assert names((await network.rooms.snapshot(record_id)).jsonld) == ["Ada"]


async def test_a_room_loaded_during_the_transaction_starts_from_the_published_version(
    served: Served, network: Network, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``crdt-ws`` loading the draft (after a restart, say) while it is being published.

    The load reads ``records`` and then ``crdt_drafts``. If the publish did not hold the
    ``records`` row from the start, the load could read the old version, find the draft gone,
    and hydrate a second draft of the old epoch from the old data; or the two would deadlock.
    """
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person("Ada"))
    await edit(network, record_id, editor.id, add_name("Second name"))
    locked, release = asyncio.Event(), asyncio.Event()
    known_reference_ids = service.known_reference_ids

    async def parked(*args: Any, **kwargs: Any) -> set[str]:
        locked.set()  # every lock is held, nothing is written yet
        await release.wait()
        return await known_reference_ids(*args, **kwargs)

    monkeypatch.setattr(service, "known_reference_ids", parked)
    publishing = asyncio.create_task(publish(served, editor, record_id))
    await asyncio.wait_for(locked.wait(), 10)
    await crash(network.rooms)  # the room leaves memory without touching the database
    loading = asyncio.create_task(network.rooms.open(record_id))
    await eventually(waiting(db_engine, 1), what="the load waits for the publish")
    release.set()

    response = await asyncio.wait_for(publishing, 20)
    assert response.status_code == 200, response.text
    room = await asyncio.wait_for(loading, 20)
    assert (room.draft_epoch, room.base_version) == (1, 1)
    assert names(room.snapshot().jsonld) == ["Ada", "Second name"]
    after = await stored(db_engine, record_id)
    assert after.record is not None and after.draft is not None
    assert (after.record.version, after.record.draft_epoch) == (1, 1)
    # The close that follows the publish names epoch 0 and leaves the new draft alone.
    assert (after.draft.draft_epoch, after.draft.base_version) == (1, 1)
    assert await network.rooms.get(record_id) is room


# -- a crash before the commit ------------------------------------------------------------------


class Crash(Exception):
    """Stands for the process dying inside the transaction."""


def crash_on_call(monkeypatch: pytest.MonkeyPatch, target: Any, name: str, call: int) -> None:
    """Make the ``call``-th call of ``target.name`` raise ``Crash`` (earlier calls are real)."""
    real = getattr(target, name)
    calls = 0

    def crashing(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == call:
            raise Crash(name)
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, crashing)


@pytest.mark.parametrize(
    ("target", "name", "call"),
    [
        (service, "_render", 1),  # before the first write
        (service, "upsert_entity_labels_for_record", 1),  # record, revision and event written
        (store, "delete_draft", 1),  # the first record complete but for its draft
        (service, "_render", 2),  # the first record complete, the second not started
        (service, "upsert_entity_labels_for_record", 2),
        (service, "_resolve_schema_findings", 2),  # the second record's label written
        (store, "delete_draft", 2),  # everything written, one statement before the commit
    ],
)
async def test_a_crash_before_the_commit_leaves_everything_as_it_was(
    served: Served,
    network: Network,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    target: Any,
    name: str,
    call: int,
) -> None:
    editor = await caller(db_engine)
    maker_id, maker_uri = await new_draft(served, editor, "Person", person("Ada"))
    await edit(network, maker_id, editor.id, add_name("A. A. Lovelace"))
    # A published record with a new draft, so that there is a published state to keep.
    object_id, object_uri = await new_draft(served, editor, "HumanMadeObject", painting("Engine"))
    assert (await publish(served, editor, object_id, NO_PRIMARY_NAME)).status_code == 200
    await network.rooms.open(
        object_id,
        content=new_record_data(
            "HumanMadeObject",
            object_uri,
            1,
            painting("Engine", maker_uri),
            label_language="en",
        ),
    )
    await edit(network, object_id, editor.id, add_name("Analytical Engine"))
    bundle = (object_id, maker_id)
    for record_id in bundle:
        await add_finding(db_engine, record_id, "LAE-SCHEMA-required")
    before = [fingerprint(await stored(db_engine, record_id)) for record_id in bundle]
    drafts = [(await network.rooms.snapshot(record_id)).jsonld for record_id in bundle]

    with monkeypatch.context() as patched:
        crash_on_call(patched, target, name, call)
        with pytest.raises(Crash):
            await publish(served, editor, object_id, NO_PRIMARY_NAME)

    assert [fingerprint(await stored(db_engine, record_id)) for record_id in bundle] == before
    assert [(await network.rooms.snapshot(record_id)).jsonld for record_id in bundle] == drafts
    assert await lock_waiters(db_engine) == 0

    # Nothing is left half done: the same publish now goes through.
    response = await publish(served, editor, object_id, NO_PRIMARY_NAME)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "version": 2,
        "published": [str(object_id), str(maker_id)],
        "draft_epoch": 2,
    }
    assert (await stored(db_engine, object_id)).counts == (2, 2, 1, 0, 0)
    assert (await stored(db_engine, maker_id)).counts == (1, 1, 1, 0, 0)
    for record_id in bundle:
        (finding,) = (await stored(db_engine, record_id)).findings
        assert (finding.status, finding.resolved_by) == (QaFindingStatus.RESOLVED, editor.id)
