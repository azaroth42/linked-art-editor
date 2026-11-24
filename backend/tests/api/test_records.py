"""``/api/records``: create, read for the editor, revisions (Phase 1, Step 1.6)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pycrdt import Doc
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from lae.api.records import (
    RECORD_CODES,
    RECORD_TYPES,
    new_record_data,
    ordered_record,
    record_etag,
)
from lae.db.models import CrdtDraft, Record, RecordStatus
from lae.ids import is_internal_node_iri
from lae.projection import project
from lae.rdf.context import load_context
from tests.api.conftest import (
    BASE_URI,
    CONTEXT_URL,
    Network,
    a_while_ago,
    auth_headers,
    id_of,
    insert_draft,
    insert_labels,
    insert_record,
    insert_revision,
    new_uri,
    node_prefix,
    titled_record,
)
from tests.auth.conftest import Served

ERRORS_JSON = Path(__file__).resolve().parents[3] / "frontend/public/locales/en/errors.json"


def node_ids(value: Any) -> list[str]:
    """Every ``id`` in ``value`` that is an internal node IRI, sorted (``jsonb`` reorders keys)."""
    return sorted(_node_ids(value))


def _node_ids(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        if is_internal_node_iri(value.get("id"), base_uri=BASE_URI):
            found.append(value["id"])
        for child in value.values():
            found.extend(_node_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_node_ids(child))
    return found


async def stored(engine: AsyncEngine, record_id: Any) -> Record:
    async with AsyncSession(engine) as session:
        record = await session.get(Record, record_id)
        assert record is not None
        return record


# --- unit -------------------------------------------------------------------------------------


def test_record_codes_have_english_messages() -> None:
    messages: dict[str, str] = json.loads(ERRORS_JSON.read_text())
    assert {key for key in messages if key.startswith("LAE-REC-")} == set(RECORD_CODES)
    for code in RECORD_CODES:
        assert messages[code].strip(), code


def test_record_types_are_classes_of_the_pinned_context() -> None:
    context = load_context(1)["@context"]
    for record_type in RECORD_TYPES:
        assert record_type in context, record_type
    assert "Name" not in RECORD_TYPES


def test_record_etag_is_independent_of_key_order() -> None:
    data = {"id": "x", "type": "Person"}
    assert record_etag(data) == record_etag({"type": "Person", "id": "x"})
    assert record_etag(data) != record_etag({**data, "_label": "y"})
    assert len(record_etag(data)) == 32


def test_ordered_record_puts_context_id_type_label_first() -> None:
    data = {"identified_by": [], "type": "Person", "_label": "x", "id": "u", "@context": "c"}
    assert list(ordered_record(data)) == ["@context", "id", "type", "_label", "identified_by"]


def test_new_record_data_rekeys_nodes_of_the_template_source() -> None:
    source = new_uri()
    template = {
        "id": source,
        "type": "Group",
        "@context": "https://example.org/other-context",
        "identified_by": [{"id": f"{source}/node/{uuid7()}", "type": "Name", "content": "A"}],
    }
    uri = new_uri()
    data = new_record_data("Person", uri, 1, template, label_language="en")
    assert data["@context"] == CONTEXT_URL
    assert (data["id"], data["type"]) == (uri, "Person")
    assert data["identified_by"][0]["id"].startswith(node_prefix(uri))
    assert data["identified_by"][0]["content"] == "A"


# --- POST /api/records ------------------------------------------------------------------------


async def test_create_record(served: Served, db_engine: AsyncEngine) -> None:
    headers = await auth_headers(db_engine, "Collaborator", "edit_draft")
    response = await served.client.post("/api/records", json={"type": "Person"}, headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"id", "uri", "draft_epoch"}
    assert body["uri"] == BASE_URI + body["id"]
    assert body["draft_epoch"] == 0
    assert response.headers["location"] == f"/api/records/{body['id']}"

    record = await stored(db_engine, id_of(body["uri"]))
    skeleton = {"@context": CONTEXT_URL, "id": body["uri"], "type": "Person"}
    assert record.data == skeleton
    assert (record.version, record.status, record.type) == (0, RecordStatus.ACTIVE, "Person")
    assert record.etag == record_etag(skeleton)
    assert record.context_version == 1
    assert record.uri == body["uri"]
    async with AsyncSession(db_engine) as session:
        # The draft was opened in crdt-ws (Step 1.8): its row exists, hydrated from the record.
        draft = await session.scalar(select(CrdtDraft).where(CrdtDraft.record_id == record.id))
        assert draft is not None
        assert (draft.draft_epoch, draft.base_version) == (0, 0)
        assert draft.created_by is not None and draft.contributors == []
        hydrated: Doc[Any] = Doc()
        hydrated.apply_update(draft.state)
        assert project(hydrated) == skeleton
    # ... so the new record is readable at once.
    read = await served.client.get(f"/api/records/{body['id']}", headers=headers)
    assert read.status_code == 200 and read.json() == skeleton


async def test_create_record_is_undone_when_the_draft_cannot_be_opened(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    headers = await auth_headers(db_engine, "Collaborator", "edit_draft")
    # Other tests leave records in the database (the corpus has a Currency), so count the change.
    currencies = select(func.count()).select_from(Record).where(Record.type == "Currency")
    async with AsyncSession(db_engine) as session:
        before = await session.scalar(currencies)
    network.crdt_down = True
    response = await served.client.post("/api/records", json={"type": "Currency"}, headers=headers)
    assert response.status_code == 503
    assert response.json()["code"] == "LAE-CRDT-009"
    async with AsyncSession(db_engine) as session:
        assert await session.scalar(currencies) == before


async def test_create_record_with_template(served: Served, db_engine: AsyncEngine) -> None:
    headers = await auth_headers(db_engine, "Editor", "edit_draft")
    template = {
        "id": "https://elsewhere.example/thing/1",
        "type": "Group",
        "identified_by": [{"type": "Name", "content": "Anonymous"}],
        "referred_to_by": [{"type": "LinguisticObject", "content": "Note"}],
    }
    response = await served.client.post(
        "/api/records", json={"type": "HumanMadeObject", "template": template}, headers=headers
    )
    assert response.status_code == 201, response.text
    uri = response.json()["uri"]
    data = (await stored(db_engine, id_of(uri))).data
    assert (data["id"], data["type"], data["@context"]) == (uri, "HumanMadeObject", CONTEXT_URL)
    assert data["identified_by"][0]["content"] == "Anonymous"
    assert data["referred_to_by"][0]["content"] == "Note"
    ids = node_ids(data)
    assert len(ids) == 2
    assert all(node_id.startswith(node_prefix(uri)) for node_id in ids)


async def test_create_record_rejects_unknown_type(served: Served, db_engine: AsyncEngine) -> None:
    headers = await auth_headers(db_engine, "Collaborator", "edit_draft")
    response = await served.client.post("/api/records", json={"type": "Name"}, headers=headers)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "LAE-REC-005"
    assert body["params"]["type"] == "Name"
    assert "Person" in body["params"]["allowed"]


async def test_create_record_rejects_template_the_room_cannot_hold(
    served: Served, db_engine: AsyncEngine
) -> None:
    headers = await auth_headers(db_engine, "Collaborator", "edit_draft")
    template = {"identified_by": [{"type": "Name", "content": "A", "_order": 1}]}
    response = await served.client.post(
        "/api/records", json={"type": "Person", "template": template}, headers=headers
    )
    assert response.status_code == 422
    assert response.json()["code"] == "LAE-REC-006"
    assert response.json()["params"] == {"reason": "LAE-PROJ-001"}


async def test_create_record_needs_edit_draft(served: Served, db_engine: AsyncEngine) -> None:
    headers = await auth_headers(db_engine, "Viewer")
    response = await served.client.post("/api/records", json={"type": "Person"}, headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == "LAE-AUTH-001"
    assert response.json()["params"]["missing"] == ["edit_draft"]


async def test_create_record_needs_authentication(served: Served) -> None:
    response = await served.client.post("/api/records", json={"type": "Person"})
    assert response.status_code == 401
    assert response.json()["code"] == "LAE-AUTH-002"


# --- GET /api/records/{id} --------------------------------------------------------------------


async def test_get_record_keeps_node_iris_and_transcludes_labels(
    served: Served, db_engine: AsyncEngine
) -> None:
    uri, type_uri = new_uri(), f"https://vocab.test/{uuid7()}"
    data = titled_record(uri, "Night Scene", "Scène de nuit", type_uri)
    await insert_record(db_engine, data)
    await insert_labels(db_engine, type_uri, {"en": "paintings", "fr": "peintures"}, "paintings")
    headers = await auth_headers(db_engine, "Viewer")

    response = await served.client.get(
        f"/api/records/{id_of(uri)}", headers={**headers, "Accept-Language": "fr-CA, fr;q=0.8"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/ld+json"
    body = response.json()
    assert list(body)[:3] == ["@context", "id", "type"]
    assert node_ids(body) == node_ids(data)
    assert len(node_ids(body)) == 4
    assert body["classified_as"][0]["_label"] == "peintures"

    english = await served.client.get(f"/api/records/{id_of(uri)}", headers=headers)
    assert english.json()["classified_as"][0]["_label"] == "paintings"
    # The draft epoch is a header; the body stays the record's JSON-LD (Step 1.17).
    assert response.headers["X-Draft-Epoch"] == "0"
    assert "draft_epoch" not in body


async def test_get_record_needs_authentication(served: Served, db_engine: AsyncEngine) -> None:
    uri = new_uri()
    await insert_record(db_engine, titled_record(uri, "A", "B", f"https://vocab.test/{uuid7()}"))
    response = await served.client.get(f"/api/records/{id_of(uri)}")
    assert response.status_code == 401
    assert response.json()["code"] == "LAE-AUTH-002"


async def test_get_unpublished_record_needs_a_draft(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    headers = await auth_headers(db_engine, "Collaborator", "edit_draft")
    created = await served.client.post("/api/records", json={"type": "Place"}, headers=headers)
    record_id = created.json()["id"]
    # Without the draft that POST opened, a never-published record is not found.
    await network.rooms.close(id_of(created.json()["uri"]), discard=True)

    response = await served.client.get(f"/api/records/{record_id}", headers=headers)
    assert response.status_code == 404
    assert response.json()["code"] == "LAE-REC-001"

    await insert_draft(db_engine, id_of(created.json()["uri"]))
    response = await served.client.get(f"/api/records/{record_id}", headers=headers)
    assert response.status_code == 200
    # The discard ended epoch 0 (ADR-0027): the editor must connect with the new one.
    assert created.json()["draft_epoch"] == 0
    assert response.headers["X-Draft-Epoch"] == "1"
    assert (await stored(db_engine, record_id)).draft_epoch == 1
    assert response.json() == {
        "@context": CONTEXT_URL,
        "id": created.json()["uri"],
        "type": "Place",
    }


async def test_get_unknown_record(served: Served, db_engine: AsyncEngine) -> None:
    headers = await auth_headers(db_engine, "Viewer")
    response = await served.client.get(f"/api/records/{uuid7()}", headers=headers)
    assert response.status_code == 404
    assert response.json()["code"] == "LAE-REC-001"


async def test_get_deleted_and_merged_records(served: Served, db_engine: AsyncEngine) -> None:
    headers = await auth_headers(db_engine, "Viewer")
    target, deleted, merged = new_uri(), new_uri(), new_uri()
    for uri in (target, deleted, merged):
        await insert_record(
            db_engine,
            titled_record(uri, "A", "B", f"https://vocab.test/{uuid7()}"),
            status={deleted: RecordStatus.DELETED, merged: RecordStatus.MERGED}.get(
                uri, RecordStatus.ACTIVE
            ),
            merged_into=id_of(target) if uri == merged else None,
        )

    gone = await served.client.get(f"/api/records/{id_of(deleted)}", headers=headers)
    assert gone.status_code == 410
    assert gone.json()["code"] == "LAE-REC-002"

    moved = await served.client.get(f"/api/records/{id_of(merged)}", headers=headers)
    assert moved.status_code == 301
    assert moved.headers["location"] == f"/api/records/{id_of(target)}"
    assert moved.json()["code"] == "LAE-REC-003"


# --- revisions --------------------------------------------------------------------------------


async def test_revisions_of_an_edited_record(served: Served, db_engine: AsyncEngine) -> None:
    uri, type_uri = new_uri(), f"https://vocab.test/{uuid7()}"
    record_id = id_of(uri)
    v2 = titled_record(uri, "Second", "Deuxième", type_uri)
    v3 = titled_record(uri, "Third", "Troisième", type_uri)
    await insert_record(db_engine, v3, version=3)
    author, editor = uuid7(), uuid7()
    when = a_while_ago()
    await insert_revision(
        db_engine,
        record_id,
        3,
        v3,
        published_by=editor,
        authors=(author, editor),
        summary="third",
        created_at=when,
    )
    await insert_revision(db_engine, record_id, 2, v2, published_by=editor, summary="second")
    headers = await auth_headers(db_engine, "Viewer")

    listing = await served.client.get(f"/api/records/{record_id}/revisions", headers=headers)
    assert listing.status_code == 200, listing.text
    revisions = listing.json()
    # Version 1 was bulk-loaded: it never had a revision row, and its JSON-LD is gone.
    assert [r["version"] for r in revisions] == [2, 3]
    assert revisions[1] == {
        "version": 3,
        "created_at": "2026-01-02T03:04:05Z",
        "authors": [str(author), str(editor)],
        "published_by": str(editor),
        "summary": "third",
    }

    second = await served.client.get(f"/api/records/{record_id}/revisions/2", headers=headers)
    assert second.status_code == 200
    assert second.headers["content-type"] == "application/ld+json"
    assert second.json()["identified_by"][0]["content"] == "Second"
    assert node_ids(second.json()) == node_ids(v2)

    for missing in (0, 1, 4):
        response = await served.client.get(
            f"/api/records/{record_id}/revisions/{missing}", headers=headers
        )
        assert response.status_code == 404, missing
        assert response.json() == {
            "code": "LAE-REC-004",
            "path": f"/api/records/{record_id}/revisions/{missing}",
            "params": {"version": missing},
        }


async def test_revisions_of_a_bulk_loaded_record(served: Served, db_engine: AsyncEngine) -> None:
    uri = new_uri()
    data = titled_record(uri, "Loaded", "Chargé", f"https://vocab.test/{uuid7()}")
    await insert_record(db_engine, data, updated_at=a_while_ago())
    headers = await auth_headers(db_engine, "Viewer")

    listing = await served.client.get(f"/api/records/{id_of(uri)}/revisions", headers=headers)
    assert listing.json() == [
        {
            "version": 1,
            "created_at": "2026-01-02T03:04:05Z",
            "authors": [],
            "published_by": None,
            "summary": "",
        }
    ]
    first = await served.client.get(f"/api/records/{id_of(uri)}/revisions/1", headers=headers)
    assert first.status_code == 200
    assert node_ids(first.json()) == node_ids(data)


async def test_revisions_of_an_unpublished_record(served: Served, db_engine: AsyncEngine) -> None:
    headers = await auth_headers(db_engine, "Collaborator", "edit_draft")
    created = await served.client.post("/api/records", json={"type": "Type"}, headers=headers)
    record_id = created.json()["id"]
    listing = await served.client.get(f"/api/records/{record_id}/revisions", headers=headers)
    assert listing.status_code == 200
    assert listing.json() == []


@pytest.mark.parametrize("path", ["/revisions", "/revisions/1"])
async def test_revisions_need_authentication(
    served: Served, db_engine: AsyncEngine, path: str
) -> None:
    uri = new_uri()
    await insert_record(db_engine, titled_record(uri, "A", "B", f"https://vocab.test/{uuid7()}"))
    response = await served.client.get(f"/api/records/{id_of(uri)}{path}")
    assert response.status_code == 401
    assert response.json()["code"] == "LAE-AUTH-002"


@pytest.mark.parametrize("path", ["/revisions", "/revisions/1"])
async def test_revisions_of_an_unknown_record(
    served: Served, db_engine: AsyncEngine, path: str
) -> None:
    headers = await auth_headers(db_engine, "Viewer")
    response = await served.client.get(f"/api/records/{uuid7()}{path}", headers=headers)
    assert response.status_code == 404
    assert response.json()["code"] == "LAE-REC-001"
