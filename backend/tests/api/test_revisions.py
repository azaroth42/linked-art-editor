"""Revision diffs, the draft diff and revert-to-revision (Phase 1, Step 1.14).

The API runs against a real room manager in this process (``network``, as in the records
tests); drafts are edited as a browser would, with a replica ``Doc`` and a Yjs update.
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

import httpx
import pytest
from pycrdt import Doc
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from lae.api import revisions
from lae.db.models import CrdtDraft, Record, RecordStatus
from lae.projection import canonical_json, ops
from tests.api.conftest import (
    AAT,
    Network,
    auth_headers,
    id_of,
    insert_labels,
    insert_record,
    insert_revision,
    new_uri,
    titled_record,
)
from tests.auth.conftest import Served


def kind_of(uri: str) -> str:
    """The URI that classifies the names of the record ``uri`` (unique to the record)."""
    return f"https://vocab.test/kind/{id_of(uri)}"


async def editor(engine: AsyncEngine) -> dict[str, str]:
    return await auth_headers(engine, "Editor", "edit_draft", "publish")


async def three_versions(engine: AsyncEngine) -> tuple[UUID, dict[str, Any], dict[str, Any]]:
    """A record at version 3 with revision rows for 2 and 3 (version 1 was bulk-loaded).

    Version 3 renamed the English title and added a second classification.
    """
    uri, type_uri = new_uri(), f"https://vocab.test/{uuid7()}"
    v2 = titled_record(uri, "Second", "Deuxième", type_uri)
    for name in v2["identified_by"]:  # a classification no other test has a label row for
        name["classified_as"] = [{"id": kind_of(uri), "type": "Type", "_label": "stale label"}]
    v3 = copy.deepcopy(v2)
    v3["identified_by"][0]["content"] = "Third"
    v3["classified_as"].append({"id": f"https://vocab.test/{uuid7()}", "type": "Type"})
    await insert_record(engine, v3, version=3)
    await insert_revision(engine, id_of(uri), 2, v2)
    await insert_revision(engine, id_of(uri), 3, v3)
    return id_of(uri), v2, v3


async def edit_draft(network: Network, record_id: UUID, edit: Any) -> None:
    """Apply ``edit(replica)`` to the record's live draft as one update from a client."""
    room = await network.rooms.open(record_id)
    replica: Doc[Any] = Doc()
    replica.apply_update(room.ydoc.get_update())
    before = replica.get_state()
    edit(replica)
    await network.rooms.apply_update(record_id, replica.get_update(before), None)


async def record_row(engine: AsyncEngine, record_id: UUID) -> Record:
    async with AsyncSession(engine) as session:
        record = await session.get(Record, record_id)
        assert record is not None
        return record


async def draft_row(engine: AsyncEngine, record_id: UUID) -> CrdtDraft | None:
    async with AsyncSession(engine) as session:
        return await session.get(CrdtDraft, record_id)


# --- GET /revisions/{m}/diff/{n} ----------------------------------------------------------------


async def test_diff_of_two_revisions(served: Served, db_engine: AsyncEngine) -> None:
    record_id, v2, v3 = await three_versions(db_engine)
    name_id = v2["identified_by"][0]["id"]
    added = v3["classified_as"][1]["id"]
    await insert_labels(db_engine, added, {"en": "Portrait", "fr": "Portrait (fr)"}, "Portrait")
    kind = v2["identified_by"][0]["classified_as"][0]["id"]
    await insert_labels(db_engine, kind, {"en": "Primary Name"}, "Primary Name")
    headers = await auth_headers(db_engine, "Viewer")

    response = await served.client.get(
        f"/api/records/{record_id}/revisions/2/diff/3",
        headers={**headers, "Accept-Language": "fr"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert {k: v for k, v in body.items() if k != "changes"} == {
        "record_id": str(record_id),
        "from_version": 2,
        "to_version": 3,
        "draft_epoch": None,
    }
    assert body["changes"] == [
        {
            "kind": "add_ref",
            "path": "classified_as",
            "parent": [],
            "property": "classified_as",
            "key": added,
            "before": None,
            "uri": added,
            "type": "Type",
            "label": "Portrait (fr)",  # for the caller's Accept-Language
            "value": {"id": added, "type": "Type"},
        },
        {
            "kind": "update_literal",
            # The stored _label of the classification is stale; the label on file wins.
            "path": "identified_by[Name:Primary Name].content",
            "parent": [
                {
                    "property": "identified_by",
                    "key": name_id,
                    "id": name_id,
                    "type": "Name",
                    "classified_as": kind,
                    "label": "Primary Name",
                }
            ],
            "property": "content",
            "op": "change",
            "key": None,
            "before": None,
            "old": "Second",
            "new": "Third",
            "text_edit": {"index": 0, "delete": 5, "insert": "Thir"},  # the "d" stays
        },
    ]

    back = await served.client.get(f"/api/records/{record_id}/revisions/3/diff/2", headers=headers)
    assert [c["kind"] for c in back.json()["changes"]] == ["remove_ref", "update_literal"]
    same = await served.client.get(f"/api/records/{record_id}/revisions/3/diff/3", headers=headers)
    assert same.json()["changes"] == []


async def test_diff_with_a_version_that_has_no_snapshot(
    served: Served, db_engine: AsyncEngine
) -> None:
    """A bulk-loaded version 1 has no revision row; once the record has moved on it is gone."""
    record_id, _, _ = await three_versions(db_engine)
    headers = await auth_headers(db_engine, "Viewer")
    for path, missing in (("1/diff/3", 1), ("2/diff/1", 1), ("2/diff/4", 4), ("0/diff/2", 0)):
        response = await served.client.get(
            f"/api/records/{record_id}/revisions/{path}", headers=headers
        )
        assert response.status_code == 404, path
        assert response.json() == {
            "code": "LAE-REC-004",
            "path": f"/api/records/{record_id}/revisions/{path}",
            "params": {"version": missing},
        }


async def test_diff_of_a_bulk_loaded_record_with_itself(
    served: Served, db_engine: AsyncEngine
) -> None:
    """While version 1 is the current version it is read from ``records.data`` (ADR-0023)."""
    uri = new_uri()
    await insert_record(db_engine, titled_record(uri, "A", "B", f"https://vocab.test/{uuid7()}"))
    headers = await auth_headers(db_engine, "Viewer")
    response = await served.client.get(
        f"/api/records/{id_of(uri)}/revisions/1/diff/1", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["changes"] == []


async def test_diff_needs_authentication_and_a_record(
    served: Served, db_engine: AsyncEngine
) -> None:
    record_id, _, _ = await three_versions(db_engine)
    anonymous = await served.client.get(f"/api/records/{record_id}/revisions/2/diff/3")
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "LAE-AUTH-002"
    headers = await auth_headers(db_engine, "Viewer")
    unknown = await served.client.get(f"/api/records/{uuid7()}/revisions/2/diff/3", headers=headers)
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "LAE-REC-001"


# --- POST /draft/diff ---------------------------------------------------------------------------


async def test_draft_diff_compares_the_published_version_with_the_live_draft(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    record_id, _, v3 = await three_versions(db_engine)
    headers = await editor(db_engine)
    path = f"/api/records/{record_id}/draft/diff"

    room = await network.rooms.open(record_id)
    untouched = await served.client.post(path, headers=headers)
    assert untouched.status_code == 200, untouched.text
    assert untouched.json() == {
        "record_id": str(record_id),
        "from_version": 3,
        "to_version": None,
        "draft_epoch": room.draft_epoch,
        "changes": [],
    }

    def edit(replica: Doc[Any]) -> None:
        ops.add_reference(replica, "classified_as", f"{AAT}300033618", "Type")
        names = ops.list_entries(replica, "identified_by")
        ops.set_scalar(replica, "content", "Third!", base=names[0][1])
        ops.remove_entry(replica, "identified_by", names[1][0])

    await edit_draft(network, record_id, edit)
    edited = await served.client.post(path, headers=headers)
    assert edited.status_code == 200, edited.text
    changes = edited.json()["changes"]
    assert [(c["kind"], c["path"]) for c in changes] == [
        ("add_ref", "classified_as"),
        ("remove_node", "identified_by"),
        # No label row for the classification: its stored _label is all there is.
        ("update_literal", "identified_by[Name:stale label].content"),
    ]
    assert changes[0]["uri"] == f"{AAT}300033618"
    assert changes[1]["node_id"] == v3["identified_by"][1]["id"]
    assert changes[1]["summary_fields"]["content"] == "Deuxième"
    assert changes[2]["text_edit"] == {"index": 5, "delete": 0, "insert": "!"}

    # Read from memory or rebuilt from the rows: the same answer after the room is evicted.
    await network.rooms.close(record_id, discard=False)
    again = await served.client.post(path, headers=headers)
    assert again.json()["changes"] == changes


async def test_draft_diff_of_a_never_published_record(
    served: Served, db_engine: AsyncEngine
) -> None:
    headers = await editor(db_engine)
    created = await served.client.post(
        "/api/records",
        json={"type": "Person", "template": {"_label": "A person"}},
        headers=headers,
    )
    record_id = created.json()["id"]
    response = await served.client.post(f"/api/records/{record_id}/draft/diff", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["from_version"], body["to_version"]) == (0, None)
    assert body["draft_epoch"] == created.json()["draft_epoch"]
    # Nothing was published: everything beside the bare record counts as added.
    assert [(c["kind"], c["path"], c["op"], c["new"]) for c in body["changes"]] == [
        ("update_literal", "_label", "set", "A person")
    ]


async def test_draft_diff_needs_publish(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    record_id, _, _ = await three_versions(db_engine)
    await network.rooms.open(record_id)
    path = f"/api/records/{record_id}/draft/diff"
    anonymous = await served.client.post(path)
    assert anonymous.status_code == 401
    collaborator = await auth_headers(db_engine, "Collaborator", "edit_draft")
    refused = await served.client.post(path, headers=collaborator)
    assert refused.status_code == 403
    assert refused.json() == {
        "code": "LAE-AUTH-001",
        "path": path,
        "params": {"required": ["publish"], "missing": ["publish"]},
    }


async def test_draft_diff_without_a_draft_a_record_or_crdt_ws(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    record_id, _, _ = await three_versions(db_engine)
    headers = await editor(db_engine)
    no_draft = await served.client.post(f"/api/records/{record_id}/draft/diff", headers=headers)
    assert no_draft.status_code == 404
    assert no_draft.json()["code"] == "LAE-CRDT-003"
    assert await draft_row(db_engine, record_id) is None  # asking creates no draft

    unknown = await served.client.post(f"/api/records/{uuid7()}/draft/diff", headers=headers)
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "LAE-REC-001"

    network.crdt_down = True
    down = await served.client.post(f"/api/records/{record_id}/draft/diff", headers=headers)
    assert down.status_code == 503
    assert down.json()["code"] == "LAE-CRDT-009"


# --- POST /revisions/{n}/revert -----------------------------------------------------------------


async def test_revert_replaces_the_draft_with_the_revision(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    record_id, v2, v3 = await three_versions(db_engine)
    headers = await editor(db_engine)
    # A draft with an unpublished edit, which the revert throws away.
    await edit_draft(
        network, record_id, lambda doc: ops.set_scalar(doc, "x_note", "unpublished edit")
    )
    old_epoch = (await network.rooms.snapshot(record_id)).draft_epoch

    response = await served.client.post(
        f"/api/records/{record_id}/revisions/2/revert", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "id": str(record_id),
        "version": 2,
        "draft_epoch": body["draft_epoch"],
        "base_version": 3,
    }
    # Discarding the old draft ended its epoch (ADR-0027): stale clients get 4409.
    assert body["draft_epoch"] > old_epoch
    record = await record_row(db_engine, record_id)
    assert (record.version, record.draft_epoch) == (3, body["draft_epoch"])
    assert canonical_json(record.data) == canonical_json(v3)  # nothing was published

    # The draft is revision 2 exactly, node IRIs included.
    snapshot = await network.rooms.snapshot(record_id)
    assert canonical_json(snapshot.jsonld) == canonical_json(v2)
    assert (snapshot.draft_epoch, snapshot.base_version) == (body["draft_epoch"], 3)

    # So the Publish dialog shows the changes from version 3 back to version 2.
    draft_diff = await served.client.post(f"/api/records/{record_id}/draft/diff", headers=headers)
    back = await served.client.get(f"/api/records/{record_id}/revisions/3/diff/2", headers=headers)
    assert draft_diff.json()["changes"] == back.json()["changes"] != []


async def test_revert_without_a_draft_and_to_the_current_version(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    record_id, _, v3 = await three_versions(db_engine)
    headers = await editor(db_engine)
    assert await draft_row(db_engine, record_id) is None
    response = await served.client.post(
        f"/api/records/{record_id}/revisions/3/revert", headers=headers
    )
    assert response.status_code == 200, response.text
    snapshot = await network.rooms.snapshot(record_id)
    assert canonical_json(snapshot.jsonld) == canonical_json(v3)
    assert snapshot.draft_epoch == response.json()["draft_epoch"]


async def test_revert_to_a_version_that_has_no_snapshot_keeps_the_draft(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    record_id, _, _ = await three_versions(db_engine)
    headers = await editor(db_engine)
    await edit_draft(network, record_id, lambda doc: ops.set_scalar(doc, "x_note", "keep me"))
    before = await network.rooms.snapshot(record_id)

    for missing in (1, 4):  # bulk-loaded version 1 has no row yet (Step 1.10 adds it)
        response = await served.client.post(
            f"/api/records/{record_id}/revisions/{missing}/revert", headers=headers
        )
        assert response.status_code == 404
        assert response.json()["code"] == "LAE-REC-004"
    after = await network.rooms.snapshot(record_id)
    assert (after.draft_epoch, after.jsonld["x_note"]) == (before.draft_epoch, "keep me")


async def test_revert_needs_publish(served: Served, db_engine: AsyncEngine) -> None:
    record_id, _, _ = await three_versions(db_engine)
    path = f"/api/records/{record_id}/revisions/2/revert"
    anonymous = await served.client.post(path)
    assert anonymous.status_code == 401
    collaborator = await auth_headers(db_engine, "Collaborator", "edit_draft")
    refused = await served.client.post(path, headers=collaborator)
    assert refused.status_code == 403
    assert refused.json()["code"] == "LAE-AUTH-001"
    assert await draft_row(db_engine, record_id) is None

    unknown = await served.client.post(
        f"/api/records/{uuid7()}/revisions/2/revert", headers=await editor(db_engine)
    )
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "LAE-REC-001"


@pytest.mark.parametrize("status", [RecordStatus.DELETED, RecordStatus.MERGED])
async def test_revert_of_a_deleted_or_merged_record(
    served: Served, db_engine: AsyncEngine, status: RecordStatus
) -> None:
    uri = new_uri()
    data = titled_record(uri, "A", "B", f"https://vocab.test/{uuid7()}")
    await insert_record(db_engine, data, status=status)
    response = await served.client.post(
        f"/api/records/{id_of(uri)}/revisions/1/revert", headers=await editor(db_engine)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "LAE-CRDT-005"


class Reopening(Network):
    """A network on which another client opens a draft right after each of the first
    ``times`` discards: what an editor's reconnecting socket does."""

    def __init__(self, inner: Network, times: int) -> None:
        super().__init__(inner.crdt_app)
        self.times = times
        self.discards = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        if request.url.path.endswith("/close"):
            self.discards += 1
            if self.discards <= self.times:
                await self.rooms.open(UUID(request.url.path.split("/")[-2]))
        return response


async def test_revert_wins_against_a_client_that_reopens_the_draft(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    record_id, v2, _ = await three_versions(db_engine)
    racing = Reopening(network, times=revisions.REVERT_ATTEMPTS - 1)
    served.app.state.http_client._transport = racing
    response = await served.client.post(
        f"/api/records/{record_id}/revisions/2/revert", headers=await editor(db_engine)
    )
    assert response.status_code == 200, response.text
    assert racing.discards == revisions.REVERT_ATTEMPTS
    snapshot = await network.rooms.snapshot(record_id)
    assert canonical_json(snapshot.jsonld) == canonical_json(v2)
    assert snapshot.draft_epoch == response.json()["draft_epoch"]


async def test_revert_gives_up_with_409_when_the_draft_keeps_coming_back(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    record_id, _, v3 = await three_versions(db_engine)
    racing = Reopening(network, times=revisions.REVERT_ATTEMPTS)
    served.app.state.http_client._transport = racing
    path = f"/api/records/{record_id}/revisions/2/revert"
    response = await served.client.post(path, headers=await editor(db_engine))
    assert response.status_code == 409
    assert response.json() == {
        "code": "LAE-CRDT-004",
        "path": path,
        "params": {"record_id": str(record_id)},
    }
    # The record is left with the other client's draft of the published version.
    snapshot = await network.rooms.snapshot(record_id)
    assert canonical_json(snapshot.jsonld) == canonical_json(v3)


async def test_revert_when_crdt_ws_is_down(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    record_id, _, _ = await three_versions(db_engine)
    network.crdt_down = True
    response = await served.client.post(
        f"/api/records/{record_id}/revisions/2/revert", headers=await editor(db_engine)
    )
    assert response.status_code == 503
    assert response.json()["code"] == "LAE-CRDT-009"
