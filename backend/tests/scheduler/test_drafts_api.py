"""``GET /api/drafts`` and ``POST /api/records/{id}/draft/extend`` (``lae.api.drafts``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from tests.api.conftest import (
    Network,
    id_of,
    insert_labels,
    insert_record,
    new_uri,
    titled_record,
)
from tests.auth.conftest import Served, bearer, create_token, create_user
from tests.scheduler.conftest import TTL_DAYS, draft_row, make_idle

TYPE_URI = "http://vocab.getty.edu/aat/300033618"


async def user_with_token(
    engine: AsyncEngine, role: str, *scopes: str, display_name: str = "Test User"
) -> tuple[UUID, dict[str, str]]:
    """A new user with ``role`` and the ``Authorization`` header of a token for it."""
    user = await create_user(engine, role, display_name=display_name)
    token, _ = await create_token(engine, user, *scopes)
    return user, bearer(token)


async def create(served: Served, headers: dict[str, str], body: dict[str, Any]) -> UUID:
    """``POST /api/records``; return the new record's id."""
    response = await served.client.post("/api/records", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def by_id(drafts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {draft["record_id"]: draft for draft in drafts}


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def test_editor_sees_every_draft_with_its_expiry(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    author, author_headers = await user_with_token(
        db_engine, "Collaborator", "edit_draft", display_name="Ada Author"
    )
    _, editor_headers = await user_with_token(db_engine, "Editor", "edit_draft", "publish")
    template = {"identified_by": [{"type": "Name", "content": "Nameless Sculptor"}]}
    created = await create(served, author_headers, {"type": "Person", "template": template})
    uri = new_uri()
    await insert_record(db_engine, titled_record(uri, "Published object", "Objet", TYPE_URI))
    await insert_labels(db_engine, uri, {"en": "Published object", "fr": "Objet publié"}, "x")
    await network.rooms.open(id_of(uri))
    await make_idle(db_engine, id_of(uri), days=2, contributors=[author])

    response = await served.client.get(
        "/api/drafts", headers={**editor_headers, "Accept-Language": "fr"}
    )

    assert response.status_code == 200, response.text
    drafts = by_id(response.json())
    new = drafts[str(created)]
    assert new["version"] == 0
    assert new["type"] == "Person"
    assert new["label"] == "Nameless Sculptor"  # from the template (no label row yet)
    assert new["created_by"] == str(author)
    assert new["draft_epoch"] == 0
    assert instant(new["expires_at"]) - instant(new["last_activity_at"]) == timedelta(days=TTL_DAYS)
    published = drafts[str(id_of(uri))]
    assert published["label"] == "Objet publié"  # entity_labels, for Accept-Language
    assert published["version"] == 1
    assert published["uri"] == uri
    assert published["contributors"] == [
        {"id": str(author), "display_name": "Ada Author", "kind": "human"}
    ]
    expires = instant(published["expires_at"])
    assert timedelta(days=2.9) < expires - datetime.now(UTC) < timedelta(days=3.1)
    order = [draft["record_id"] for draft in response.json()]
    assert order.index(str(id_of(uri))) < order.index(str(created))  # closest expiry first


async def test_collaborator_sees_only_own_drafts(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    me, my_headers = await user_with_token(db_engine, "Collaborator", "edit_draft")
    _, other_headers = await user_with_token(db_engine, "Collaborator", "edit_draft")
    mine = await create(served, my_headers, {"type": "Place"})
    theirs = await create(served, other_headers, {"type": "Place"})
    contributed = await create(served, other_headers, {"type": "Place"})
    await make_idle(db_engine, contributed, days=1, contributors=[me])

    response = await served.client.get("/api/drafts", headers=my_headers)

    assert response.status_code == 200, response.text
    seen = set(by_id(response.json()))
    assert {str(mine), str(contributed)} <= seen
    assert str(theirs) not in seen
    contributors = by_id(response.json())[str(contributed)]["contributors"]
    assert [c["id"] for c in contributors] == [str(me)]


async def test_unknown_contributor_is_listed_without_a_name(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    _, headers = await user_with_token(db_engine, "Editor", "edit_draft", "publish")
    created = await create(served, headers, {"type": "Type"})
    ghost = uuid7()
    await make_idle(db_engine, created, days=0, contributors=[ghost])
    response = await served.client.get("/api/drafts", headers=headers)
    assert by_id(response.json())[str(created)]["contributors"] == [
        {"id": str(ghost), "display_name": None, "kind": None}
    ]


async def test_listing_drafts_needs_edit_draft(served: Served, db_engine: AsyncEngine) -> None:
    _, viewer = await user_with_token(db_engine, "Viewer")
    response = await served.client.get("/api/drafts", headers=viewer)
    assert response.status_code == 403
    assert response.json()["code"] == "LAE-AUTH-001"
    anonymous = await served.client.get("/api/drafts")
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "LAE-AUTH-002"


async def test_extend_resets_last_activity(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    _, author = await user_with_token(db_engine, "Collaborator", "edit_draft")
    _, other = await user_with_token(db_engine, "Collaborator", "edit_draft")
    created = await create(served, author, {"type": "HumanMadeObject"})
    await make_idle(db_engine, created, days=4)
    before = await draft_row(db_engine, created)
    assert before is not None

    # Any collaborator may extend (editing would reset the clock as well).
    response = await served.client.post(f"/api/records/{created}/draft/extend", headers=other)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["record_id"] == str(created)
    after = await draft_row(db_engine, created)
    assert after is not None
    assert after.last_activity_at > before.last_activity_at + timedelta(days=3)
    assert instant(body["last_activity_at"]) == after.last_activity_at
    assert instant(body["expires_at"]) == after.last_activity_at + timedelta(days=TTL_DAYS)
    assert after.draft_epoch == before.draft_epoch  # not a new draft


async def test_extend_without_a_draft_or_a_record_is_404(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    _, headers = await user_with_token(db_engine, "Editor", "edit_draft", "publish")
    uri = new_uri()
    await insert_record(db_engine, titled_record(uri, "No draft", "Pas de brouillon", TYPE_URI))

    no_draft = await served.client.post(f"/api/records/{id_of(uri)}/draft/extend", headers=headers)
    assert no_draft.status_code == 404
    assert no_draft.json()["code"] == "LAE-CRDT-003"

    no_record = await served.client.post(f"/api/records/{uuid7()}/draft/extend", headers=headers)
    assert no_record.status_code == 404
    assert no_record.json()["code"] == "LAE-REC-001"


async def test_extend_needs_edit_draft(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    _, author = await user_with_token(db_engine, "Collaborator", "edit_draft")
    created = await create(served, author, {"type": "Person"})
    _, viewer = await user_with_token(db_engine, "Viewer")
    response = await served.client.post(f"/api/records/{created}/draft/extend", headers=viewer)
    assert response.status_code == 403
    assert response.json()["code"] == "LAE-AUTH-001"
