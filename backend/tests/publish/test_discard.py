"""``POST /api/records/{id}/draft/discard`` (Step 1.10; decisions B4, B6; ADR-0027)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from lae.crdt.client import CrdtWsClient
from lae.projection import ops
from tests.publish.conftest import (
    BeforeClose,
    Caller,
    Network,
    Served,
    caller,
    edit,
    new_draft,
    open_draft,
    person,
    publish,
    stored,
)


def add_name(content: str) -> Any:
    return lambda doc: ops.add_node(doc, "identified_by", {"type": "Name", "content": content})


async def discard(
    app: Served, headers: dict[str, str], record_id: UUID, draft_epoch: int | None = None
) -> Any:
    """``POST …/draft/discard``, with the epoch of the draft the caller means if given."""
    body = None if draft_epoch is None else {"draft_epoch": draft_epoch}
    return await app.client.post(
        f"/api/records/{record_id}/draft/discard", headers=headers, json=body
    )


async def names_in_draft(network: Network, record_id: UUID) -> list[str]:
    snapshot = await network.rooms.snapshot(record_id)
    return [name["content"] for name in snapshot.jsonld["identified_by"]]


async def publish_and_edit_again(
    served: Served, network: Network, editor: Caller, record_id: UUID
) -> int:
    """Publish the draft, then open the next one and edit it; return the new draft's epoch."""
    assert (await publish(served, editor, record_id)).status_code == 200
    epoch = await open_draft(network, record_id)
    await edit(network, record_id, editor.id, add_name("Written after the publish"))
    return epoch


async def test_discarding_the_draft_of_a_new_record_deletes_the_record(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    record_id, uri = await new_draft(served, editor, "Person", person())
    await edit(network, record_id, editor.id, add_name("Second name"))

    response = await discard(served, editor.headers, record_id)

    assert response.status_code == 200, response.text
    assert response.json() == {"id": str(record_id), "deleted": True, "draft_epoch": None}
    after = await stored(db_engine, record_id, uri)
    assert after.record is None and after.counts == (0, 0, 0, 0, 0)
    assert await network.rooms.get(record_id) is None
    gone = await served.client.get(f"/api/records/{record_id}", headers=editor.headers)
    assert gone.status_code == 404
    again = await discard(served, editor.headers, record_id)
    assert (again.status_code, again.json()["code"]) == (404, "LAE-REC-001")


async def test_discarding_the_draft_of_a_published_record_keeps_the_published_version(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person("Ada"))
    assert (await publish(served, editor, record_id)).status_code == 200
    published = await stored(db_engine, record_id)
    assert await open_draft(network, record_id) == 1
    await edit(network, record_id, editor.id, add_name("Not wanted"))
    assert (await stored(db_engine, record_id)).counts == (1, 1, 1, 1, 1)

    response = await discard(served, editor.headers, record_id)

    assert response.json() == {"id": str(record_id), "deleted": False, "draft_epoch": 2}
    after = await stored(db_engine, record_id)
    assert after.counts == (1, 1, 1, 0, 0)
    assert after.record is not None and published.record is not None
    assert (after.record.version, after.record.draft_epoch) == (1, 2)
    assert after.record.data == published.record.data
    assert after.record.updated_at == published.record.updated_at
    assert await network.rooms.get(record_id) is None
    # The next draft starts from the published version, at the new epoch.
    assert await open_draft(network, record_id) == 2
    names = (await network.rooms.snapshot(record_id)).jsonld["identified_by"]
    assert [name["content"] for name in names] == ["Ada"]

    # Without a draft there is nothing to discard, and that is not an error.
    await network.rooms.close(record_id, discard=True)
    nothing = await discard(served, editor.headers, record_id)
    assert nothing.json() == {"id": str(record_id), "deleted": False, "draft_epoch": 3}


async def test_discard_needs_the_publish_permission(served: Served, db_engine: AsyncEngine) -> None:
    collaborator = await caller(db_engine, "Collaborator")
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, collaborator, "Person", person())

    refused = await discard(served, collaborator.headers, record_id)
    assert (refused.status_code, refused.json()["code"]) == (403, "LAE-AUTH-001")
    anonymous = await served.client.post(f"/api/records/{record_id}/draft/discard")
    assert anonymous.status_code == 401
    assert (await stored(db_engine, record_id)).counts == (0, 0, 0, 1, 0)

    unknown = await discard(served, editor.headers, UUID(str(uuid7())))
    assert (unknown.status_code, unknown.json()["code"]) == (404, "LAE-REC-001")


async def test_nothing_is_discarded_while_crdt_ws_is_unreachable(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person())
    network.crdt_down = True

    response = await discard(served, editor.headers, record_id)

    assert (response.status_code, response.json()["code"]) == (503, "LAE-CRDT-009")
    after = await stored(db_engine, record_id)
    assert after.record is not None and after.counts == (0, 0, 0, 1, 0)


async def test_a_new_record_is_kept_if_a_draft_was_opened_again_meanwhile(
    served: Served, network: Network, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the row would cascade to the new draft and leave its room without a record."""
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person())
    close = CrdtWsClient.close

    async def close_then_reopen(self: CrdtWsClient, *args: Any, **kwargs: Any) -> None:
        await close(self, *args, **kwargs)
        await network.rooms.open(record_id)  # somebody reconnects at once

    monkeypatch.setattr(CrdtWsClient, "close", close_then_reopen)

    response = await discard(served, editor.headers, record_id)

    assert response.json() == {"id": str(record_id), "deleted": False, "draft_epoch": 1}
    after = await stored(db_engine, record_id)
    assert after.record is not None and after.draft is not None
    assert (after.record.version, after.draft.draft_epoch) == (0, 1)


async def test_a_late_discard_leaves_the_draft_opened_after_a_publish(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    """Issue #178: the user discarded the draft of epoch 0; the request arrives after that draft
    was published and somebody has started the next one."""
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person("Ada"))
    seen = (await network.rooms.snapshot(record_id)).draft_epoch
    newer = await publish_and_edit_again(served, network, editor, record_id)
    assert newer == seen + 1
    before = await stored(db_engine, record_id)

    path = f"/api/records/{record_id}/draft/discard"
    response = await discard(served, editor.headers, record_id, draft_epoch=seen)

    assert response.status_code == 409, response.text
    assert response.json() == {
        "code": "LAE-CRDT-007",
        "path": path,
        "params": {"record_id": str(record_id)},
    }
    after = await stored(db_engine, record_id)
    assert after.record is not None and after.draft is not None
    assert (after.record.version, after.record.draft_epoch, after.draft.draft_epoch) == (
        1,
        newer,
        newer,
    )
    assert after.counts == before.counts
    assert await names_in_draft(network, record_id) == ["Ada", "Written after the publish"]
    room = await network.rooms.get(record_id)
    assert room is not None and room.draft_epoch == newer  # nobody was disconnected

    # Naming the draft there is discards it.
    response = await discard(served, editor.headers, record_id, draft_epoch=newer)
    assert response.json() == {"id": str(record_id), "deleted": False, "draft_epoch": newer + 1}
    assert (await stored(db_engine, record_id)).draft is None


@pytest.mark.parametrize("named", [False, True], ids=["no_epoch", "epoch"])
@pytest.mark.parametrize("reopened", [True, False], ids=["reopened", "not_reopened"])
async def test_a_publish_during_the_discard_is_not_undone_and_its_next_draft_is_kept(
    served: Served, network: Network, db_engine: AsyncEngine, named: bool, reopened: bool
) -> None:
    """The publish lands after the endpoint has read the record and before ``crdt-ws`` is asked
    to discard: the discard is limited to the epoch the endpoint read, with or without one in
    the request, and the caller learns that the draft went another way."""
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person("Ada"))
    seen = (await network.rooms.snapshot(record_id)).draft_epoch

    async def publish_meanwhile() -> None:
        if reopened:
            await publish_and_edit_again(served, network, editor, record_id)
        else:
            assert (await publish(served, editor, record_id)).status_code == 200

    served.app.state.http_client._transport = BeforeClose(network, publish_meanwhile)
    response = await discard(served, editor.headers, record_id, seen if named else None)

    assert (response.status_code, response.json()["code"]) == (409, "LAE-CRDT-007")
    after = await stored(db_engine, record_id)
    # Published once, and not deleted as a never-published record.
    assert after.record is not None
    assert (after.record.version, after.record.draft_epoch) == (1, seen + 1)
    assert (len(after.revisions), len(after.events)) == (1, 1)
    if reopened:
        assert after.draft is not None and after.draft.draft_epoch == seen + 1
        assert await names_in_draft(network, record_id) == ["Ada", "Written after the publish"]
    else:
        assert after.draft is None


async def test_a_discard_that_names_an_ended_epoch_is_refused(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    """A second click, or a tab that missed somebody else's discard: 409, nothing touched."""
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person("Ada"))
    assert (await publish(served, editor, record_id)).status_code == 200
    seen = await open_draft(network, record_id)
    first = await discard(served, editor.headers, record_id, draft_epoch=seen)
    assert first.json() == {"id": str(record_id), "deleted": False, "draft_epoch": seen + 1}
    await open_draft(network, record_id)
    await edit(network, record_id, editor.id, add_name("Started over"))

    again = await discard(served, editor.headers, record_id, draft_epoch=seen)

    assert (again.status_code, again.json()["code"]) == (409, "LAE-CRDT-007")
    assert await names_in_draft(network, record_id) == ["Ada", "Started over"]
    # A wrong type is the request's fault, not a conflict.
    malformed = await served.client.post(
        f"/api/records/{record_id}/draft/discard",
        headers=editor.headers,
        json={"draft_epoch": "latest"},
    )
    assert malformed.status_code == 422
    assert await names_in_draft(network, record_id) == ["Ada", "Started over"]
