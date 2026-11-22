"""``POST /api/validate`` and ``POST /api/records/{id}/draft/validate`` (Step 1.9)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.api.validation import SNAPSHOT_SOURCE_STATE
from lae.auth import ROLE_PERMISSIONS
from lae.db.models import EntityLabel, Record, RecordStatus
from lae.ids import new_uuid7, record_uri
from lae.projection.hydrate import assign_node_iris
from lae.validation import engine
from lae.validation.shacl import ShapesError
from tests.auth.conftest import bearer, create_token, create_user, serve_app
from tests.validation.conftest import base

BASE_URI = "https://lae.test/id/"


@dataclass
class Snapshot:
    """The part of Step 1.8's snapshot that validation reads."""

    jsonld: dict[str, Any]


@dataclass
class FakeDrafts:
    """A ``DraftSnapshotSource``: drafts by record id; ``LookupError`` for ids in ``missing``."""

    drafts: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    missing: set[UUID] = field(default_factory=set)
    calls: list[UUID] = field(default_factory=list)

    async def snapshot(self, record_id: UUID) -> Snapshot | None:
        self.calls.append(record_id)
        if record_id in self.missing:
            raise LookupError(record_id)
        jsonld = self.drafts.get(record_id)
        return None if jsonld is None else Snapshot(jsonld)


async def token_for(engine: AsyncEngine, role: str) -> dict[str, str]:
    """Bearer header of a new user with ``role``; the token carries all the role's permissions."""
    user_id = await create_user(engine, role)
    plaintext, _ = await create_token(engine, user_id, *sorted(ROLE_PERMISSIONS[role]))
    return bearer(plaintext)


def draft(record_id: UUID, *operations: list[Any]) -> dict[str, Any]:
    """A projected draft of a local record: local URI, internal node IRIs on every node."""
    uri = record_uri(record_id, base_uri=BASE_URI)
    return assign_node_iris(base("HumanMadeObject", ["/id", uri], *operations), uri)


def summary(body: dict[str, Any]) -> list[tuple[str, str, str]]:
    return sorted((item["code"], item["severity"], item["path"]) for item in body["items"])


# --- POST /api/validate ---------------------------------------------------------------------


async def test_validate_requires_authentication(db_url: str) -> None:
    async with serve_app(db_url) as served:
        response = await served.client.post("/api/validate", json={"jsonld": base()})
    assert response.status_code == 401
    assert response.json() == {"code": "LAE-AUTH-002", "path": "/api/validate", "params": {}}


async def test_any_authenticated_user_can_validate(db_url: str, db_engine: AsyncEngine) -> None:
    headers = await token_for(db_engine, "Viewer")
    async with serve_app(db_url) as served:
        response = await served.client.post(
            "/api/validate", json={"jsonld": base()}, headers=headers
        )
    assert response.status_code == 200
    assert response.json() == {"items": [], "ok": True}


async def test_validate_returns_structured_diagnostics(db_url: str, db_engine: AsyncEngine) -> None:
    headers = await token_for(db_engine, "Viewer")
    document = base(
        "HumanMadeObject",
        ["/identified_by", None],
        ["/produced_by/timespan/begin_of_the_begin", "1700-01-01T00:00:00Z"],
    )
    async with serve_app(db_url) as served:
        response = await served.client.post(
            "/api/validate", json={"jsonld": document}, headers=headers
        )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["items"] == [
        {
            "code": "LAE-RULE-001",
            "path": "/produced_by/timespan",
            "severity": "violation",
            "params": {"begin": "1700-01-01T00:00:00Z", "end": "1642-12-31T23:59:59Z"},
            "message_en": (
                "The earliest start (1700-01-01T00:00:00Z) is later than the latest end "
                "(1642-12-31T23:59:59Z)."
            ),
        },
        {
            "code": "LAE-SHACL-001",
            "path": "/identified_by",
            "severity": "warning",
            "params": {},
            "message_en": "An object should have at least one Name in identified_by.",
        },
    ]


async def test_validate_reports_schema_failures_as_a_result(
    db_url: str, db_engine: AsyncEngine
) -> None:
    headers = await token_for(db_engine, "Viewer")
    async with serve_app(db_url) as served:
        not_a_record = await served.client.post(
            "/api/validate", json={"jsonld": {"type": "Name"}}, headers=headers
        )
        no_body = await served.client.post("/api/validate", json={}, headers=headers)
        not_an_object = await served.client.post(
            "/api/validate", json={"jsonld": []}, headers=headers
        )
    assert not_a_record.status_code == 200
    assert summary(not_a_record.json()) == [("LAE-SCHEMA-recordType", "violation", "/type")]
    assert not_a_record.json()["ok"] is False
    assert no_body.status_code == 422
    assert not_an_object.status_code == 422


async def test_validate_uses_entity_labels_and_record_status(
    db_url: str, db_engine: AsyncEngine
) -> None:
    """The database decides the class of a reference and whether its target is deleted."""
    material = f"https://example.org/material/{new_uuid7()}"
    person = f"https://example.org/person/{new_uuid7()}"
    async with AsyncSession(db_engine) as session:
        session.add(EntityLabel(uri=material, type="Type", default_label="not a material"))
        deleted = Record(uri=person, type="Person", etag="e", data={}, context_version=1)
        deleted.status = RecordStatus.DELETED
        session.add(deleted)
        await session.commit()
    document = base(
        "HumanMadeObject",
        ["/made_of/0/id", material],
        ["/produced_by/part/0/carried_out_by/0/id", person],
    )
    headers = await token_for(db_engine, "Collaborator")
    async with serve_app(db_url) as served:
        response = await served.client.post(
            "/api/validate", json={"jsonld": document}, headers=headers
        )
    assert response.status_code == 200
    assert summary(response.json()) == [
        ("LAE-RULE-004", "violation", "/produced_by/part/0/carried_out_by/0"),
        ("LAE-SHACL-006", "violation", "/made_of/0"),
    ]


async def test_validate_takes_a_record_uri(db_url: str, db_engine: AsyncEngine) -> None:
    headers = await token_for(db_engine, "Viewer")
    body = {"jsonld": base(), "record_uri": "https://example.org/object/another"}
    async with serve_app(db_url) as served:
        response = await served.client.post("/api/validate", json=body, headers=headers)
    assert summary(response.json()) == [("LAE-RDF-002", "violation", "")]


# --- POST /api/records/{id}/draft/validate --------------------------------------------------


async def test_draft_validate_requires_edit_draft(db_url: str, db_engine: AsyncEngine) -> None:
    record_id = new_uuid7()
    path = f"/api/records/{record_id}/draft/validate"
    viewer = await token_for(db_engine, "Viewer")
    drafts = FakeDrafts({record_id: draft(record_id)})
    async with serve_app(db_url, base_uri=BASE_URI) as served:
        setattr(served.app.state, SNAPSHOT_SOURCE_STATE, drafts)
        anonymous = await served.client.post(path)
        forbidden = await served.client.post(path, headers=viewer)
    assert anonymous.status_code == 401
    assert anonymous.json() == {"code": "LAE-AUTH-002", "path": path, "params": {}}
    assert forbidden.status_code == 403
    assert forbidden.json() == {
        "code": "LAE-AUTH-001",
        "path": path,
        "params": {"required": ["edit_draft"], "missing": ["edit_draft"]},
    }
    assert drafts.calls == [], "no draft is read before the permission check"


async def test_draft_validate_validates_the_snapshot(db_url: str, db_engine: AsyncEngine) -> None:
    valid_id, broken_id = new_uuid7(), new_uuid7()
    drafts = FakeDrafts(
        {
            valid_id: draft(valid_id),
            broken_id: draft(
                broken_id,
                ["/identified_by/1/content", " "],
                ["/produced_by/timespan/end_of_the_end", None],
            ),
        }
    )
    headers = await token_for(db_engine, "Collaborator")
    async with serve_app(db_url, base_uri=BASE_URI) as served:
        setattr(served.app.state, SNAPSHOT_SOURCE_STATE, drafts)
        valid = await served.client.post(f"/api/records/{valid_id}/draft/validate", headers=headers)
        broken = await served.client.post(
            f"/api/records/{broken_id}/draft/validate", headers=headers
        )
    assert valid.status_code == 200
    assert valid.json() == {"items": [], "ok": True}
    assert broken.status_code == 200
    assert summary(broken.json()) == [
        ("LAE-RULE-003", "violation", "/identified_by/1/content"),
        ("LAE-SHACL-014", "warning", "/produced_by/timespan"),
    ]
    assert broken.json()["ok"] is False
    assert drafts.calls == [valid_id, broken_id]
    # Internal node IRIs are in the draft; diagnostics address nodes by JSON Pointer only.
    assert "/node/" not in broken.text


async def test_draft_validate_checks_the_draft_against_its_record_uri(
    db_url: str, db_engine: AsyncEngine
) -> None:
    """A snapshot whose root id is another record's must not validate as this record."""
    record_id, other_id = new_uuid7(), new_uuid7()
    drafts = FakeDrafts({record_id: draft(other_id)})
    headers = await token_for(db_engine, "Collaborator")
    async with serve_app(db_url, base_uri=BASE_URI) as served:
        setattr(served.app.state, SNAPSHOT_SOURCE_STATE, drafts)
        response = await served.client.post(
            f"/api/records/{record_id}/draft/validate", headers=headers
        )
    assert response.status_code == 200
    assert response.json()["ok"] is False


async def test_draft_validate_without_a_draft_is_404(db_url: str, db_engine: AsyncEngine) -> None:
    no_draft, lookup_error = new_uuid7(), new_uuid7()
    drafts = FakeDrafts(missing={lookup_error})
    headers = await token_for(db_engine, "Collaborator")
    async with serve_app(db_url, base_uri=BASE_URI) as served:
        setattr(served.app.state, SNAPSHOT_SOURCE_STATE, drafts)
        responses = {
            record_id: await served.client.post(
                f"/api/records/{record_id}/draft/validate", headers=headers
            )
            for record_id in (no_draft, lookup_error)
        }
        malformed = await served.client.post(
            "/api/records/not-a-uuid/draft/validate", headers=headers
        )
    for record_id, response in responses.items():
        assert response.status_code == 404
        assert response.json() == {
            "code": "LAE-VALID-002",
            "path": f"/api/records/{record_id}/draft/validate",
            "params": {"id": str(record_id)},
        }
    assert malformed.status_code == 422


async def test_draft_validate_without_a_snapshot_source_is_503(
    db_url: str, db_engine: AsyncEngine
) -> None:
    """Until Step 1.8's ``CrdtWsClient`` is installed on ``app.state``."""
    record_id = new_uuid7()
    headers = await token_for(db_engine, "Collaborator")
    async with serve_app(db_url, base_uri=BASE_URI) as served:
        assert not hasattr(served.app.state, SNAPSHOT_SOURCE_STATE)
        response = await served.client.post(
            f"/api/records/{record_id}/draft/validate", headers=headers
        )
    assert response.status_code == 503
    assert response.json() == {
        "code": "LAE-VALID-001",
        "path": f"/api/records/{record_id}/draft/validate",
        "params": {},
    }


# --- startup --------------------------------------------------------------------------------


async def test_a_broken_shapes_file_stops_the_start(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> None:
        raise ShapesError("urn:lae:shape:x must have exactly one lae:code LAE-SHACL-NNN: []")

    monkeypatch.setattr(engine, "shapes_graph", broken)
    with pytest.raises(ShapesError):
        async with serve_app(db_url):
            pytest.fail("the application started with shapes that break the code rules")
