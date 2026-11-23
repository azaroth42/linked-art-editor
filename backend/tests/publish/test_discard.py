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


async def discard(app: Served, headers: dict[str, str], record_id: UUID) -> Any:
    return await app.client.post(f"/api/records/{record_id}/draft/discard", headers=headers)


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
