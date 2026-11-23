"""Fixtures and helpers for the publish tests (Phase 1 Step 1.10).

The tests drive the real stack in one process: the ``api`` application (``served``) calls a real
``crdt-ws`` application (``network``) over its internal RPC, both on the test database. Drafts
are created through ``POST /api/records`` or ``open_draft`` and edited with ``edit``, which goes
through the room manager like an update from a socket (persisted, then applied).

Every test uses fresh record ids; all tests share one database, so assertions count rows of the
records a test created, never whole tables.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import orjson
from pycrdt import Doc
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.auth import ROLE_PERMISSIONS
from lae.db.models import (
    CrdtDraft,
    CrdtUpdate,
    EntityLabel,
    OutboxEvent,
    Record,
    RecordRevision,
)
from lae.rdf import decompress, parse_ntriples
from tests.api.conftest import (  # noqa: F401  (fixtures)
    BASE_URI,
    Network,
    id_of,
    network,
    new_uri,
    served,
)
from tests.auth.conftest import Served, bearer, create_token, create_user
from tests.crdt.helpers import change, replica_of

AAT = "http://vocab.getty.edu/aat/"
SPACED_IRI = "https://commons.example.org/wiki/File:Self Portrait.jpg"
"""An ``id`` RDF has to percent-encode: the warning ``LAE-RDF-006`` (ADR-0028)."""

REPAIR_WARNING = "LAE-RDF-006"


@dataclass(frozen=True)
class Caller:
    """A user and the bearer header of a token carrying every permission of the user's role."""

    id: UUID
    headers: dict[str, str]


async def caller(engine: AsyncEngine, role: str = "Editor") -> Caller:
    """Create a user with ``role`` and a token for it."""
    user_id = await create_user(engine, role)
    token, _ = await create_token(engine, user_id, *sorted(ROLE_PERMISSIONS[role]))
    return Caller(user_id, bearer(token))


def person(name: str = "Ada Lovelace") -> dict[str, Any]:
    """Template of a ``Person`` that validates without any diagnostic."""
    return {"_label": name, "identified_by": [{"type": "Name", "content": name}]}


def person_with_warning(name: str = "Ada Lovelace") -> dict[str, Any]:
    """Template of a ``Person`` with exactly one warning: ``LAE-RDF-006`` at ``WARNING_PATH``."""
    return {**person(name), "classified_as": [{"id": SPACED_IRI, "type": "Type", "_label": "x"}]}


WARNING_PATH = "/classified_as/0/id"


def person_with_violation() -> dict[str, Any]:
    """Template of a ``Person`` with a violation: an empty ``content`` (``LAE-RULE-003``)."""
    return {"_label": "Nobody", "identified_by": [{"type": "Name", "content": ""}]}


def painting(title: str, *makers: str) -> dict[str, Any]:
    """Template of a ``HumanMadeObject`` made by the ``Person`` records at ``makers`` (URIs).

    It has one warning of its own, ``LAE-SHACL-002`` at ``/identified_by`` (no primary name).
    """
    template: dict[str, Any] = {
        "_label": title,
        "identified_by": [{"type": "Name", "content": title}],
    }
    if makers:
        template["produced_by"] = {
            "type": "Production",
            "carried_out_by": [
                {"id": maker, "type": "Person", "_label": "a stale label"} for maker in makers
            ],
        }
    return template


NO_PRIMARY_NAME = {"code": "LAE-SHACL-002", "path": "/identified_by"}
"""The warning every ``painting`` has."""


async def new_draft(
    app: Served, who: Caller, record_type: str, template: dict[str, Any]
) -> tuple[UUID, str]:
    """Create a never-published record with a draft through the API; return ``(id, uri)``."""
    response = await app.client.post(
        "/api/records", json={"type": record_type, "template": template}, headers=who.headers
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return UUID(body["id"]), body["uri"]


async def open_draft(net: Network, record_id: UUID) -> int:
    """Make sure the record has a draft (hydrated from ``records.data``); return its epoch."""
    room = await net.rooms.open(record_id)
    return room.draft_epoch


async def edit(
    net: Network, record_id: UUID, user_id: UUID | None, operation: Callable[[Doc[Any]], object]
) -> None:
    """Apply one edit to the record's draft as ``user_id``, the way a socket's update is."""
    room = await net.rooms.open(record_id)
    await net.rooms.apply_update(record_id, change(replica_of(room.ydoc), operation), user_id)


async def publish(
    app: Served,
    who: Caller,
    record_id: UUID,
    *accepted: dict[str, Any],
    summary: str = "A summary",
) -> Any:
    """``POST /api/records/{id}/publish``; return the response."""
    return await app.client.post(
        f"/api/records/{record_id}/publish",
        json={"summary": summary, "accepted_warnings": list(accepted)},
        headers=who.headers,
    )


@dataclass
class Stored:
    """Everything the database holds about one record."""

    record: Record | None
    revisions: list[RecordRevision]
    events: list[OutboxEvent]
    label: EntityLabel | None
    draft: CrdtDraft | None
    updates: int

    @property
    def counts(self) -> tuple[int, int, int, int, int]:
        """Rows in ``record_revisions``, ``outbox_events``, ``entity_labels``, ``crdt_drafts``
        and ``crdt_updates``."""
        return (
            len(self.revisions),
            len(self.events),
            int(self.label is not None),
            int(self.draft is not None),
            self.updates,
        )

    def revision(self, version: int) -> RecordRevision:
        return next(revision for revision in self.revisions if revision.version == version)

    def jsonld(self, version: int) -> dict[str, Any]:
        """The JSON-LD snapshot stored for ``version``."""
        document: dict[str, Any] = orjson.loads(decompress(self.revision(version).jsonld_zstd))
        return document

    def triples(self, version: int) -> frozenset[str]:
        """The N-Triples stored for ``version``."""
        return parse_ntriples(decompress(self.revision(version).ntriples_zstd))


async def stored(engine: AsyncEngine, record_id: UUID, uri: str | None = None) -> Stored:
    """Read every row of one record (``uri`` is needed for the label once the record is gone)."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        record = await session.get(Record, record_id)
        revisions = await session.scalars(
            select(RecordRevision)
            .where(RecordRevision.record_id == record_id)
            .order_by(RecordRevision.version)
        )
        events = await session.scalars(
            select(OutboxEvent).where(OutboxEvent.record_id == record_id).order_by(OutboxEvent.id)
        )
        label_uri = uri if record is None else record.uri
        label = None if label_uri is None else await session.get(EntityLabel, label_uri)
        updates = await session.scalar(
            select(func.count()).select_from(CrdtUpdate).where(CrdtUpdate.record_id == record_id)
        )
        return Stored(
            record=record,
            revisions=list(revisions),
            events=list(events),
            label=label,
            draft=await session.get(CrdtDraft, record_id),
            updates=int(updates or 0),
        )


__all__ = ["BASE_URI", "Network", "Served", "id_of", "new_uri"]
