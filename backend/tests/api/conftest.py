"""Fixtures for the records API and public dereference tests (Phase 1, Step 1.6).

Records are written straight into the tables (publish arrives in Step 1.10): ``insert_record``
for ``records``, ``insert_revision`` for ``record_revisions`` (zstd JSON-LD as publish stores it),
``insert_labels`` for ``entity_labels`` and ``insert_draft`` for ``crdt_drafts``. Every test uses
fresh UUIDv7 ids and unique reference URIs, because all tests share one database.

The application runs with ``base_uri = BASE_URI``, a host other than the client's
``https://api.test``, so redirects to record URIs can be told apart from paths on the API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import orjson
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from lae.api.records import record_etag
from lae.corpus import CORPUS_DIR, load_record
from lae.db.models import CrdtDraft, EntityLabel, Record, RecordRevision, RecordStatus
from lae.ids import NODE_PATH, record_id_from_uri, record_uri
from lae.projection import assign_node_iris
from lae.rdf.canonical import compress
from tests.auth.conftest import Served, bearer, create_token, create_user, serve_app

BASE_URI = "https://data.test/id/"
"""``Settings.base_uri`` of the application under test."""

CONTEXT_URL = "https://linked.art/ns/v1/linked-art.json"
PROFILE_CONTENT_TYPE = 'application/ld+json;profile="https://linked.art/ns/v1/linked-art.json"'

AAT = "http://vocab.getty.edu/aat/"
PRIMARY_NAME = {"id": f"{AAT}300404670", "type": "Type", "_label": "Primary Name"}
ENGLISH = {"id": f"{AAT}300388277", "type": "Language", "_label": "English"}
FRENCH = {"id": f"{AAT}300388306", "type": "Language", "_label": "French"}


@pytest.fixture
async def served(db_url: str) -> AsyncIterator[Served]:
    """The application (no dev user, OIDC against the fake provider) and a client for it."""
    async with serve_app(db_url, base_uri=BASE_URI) as running:
        yield running


def new_uri() -> str:
    """A fresh record URI under ``BASE_URI``."""
    return record_uri(uuid7(), base_uri=BASE_URI)


def id_of(uri: str) -> UUID:
    """The record id of a record URI under ``BASE_URI``."""
    record_id = record_id_from_uri(uri, base_uri=BASE_URI)
    assert record_id is not None
    return record_id


def node_prefix(uri: str) -> str:
    """``<uri>/node/``: every internal (and derived) node IRI of the record starts with it."""
    return uri + NODE_PATH


def titled_record(uri: str, english: str, french: str, type_uri: str) -> dict[str, Any]:
    """A small record with English and French primary names, a classification, and a date.

    Every embedded node (the two Names, the Production, its TimeSpan) has an internal node IRI.
    """
    data = {
        "@context": CONTEXT_URL,
        "id": uri,
        "type": "HumanMadeObject",
        "_label": english,
        "classified_as": [{"id": type_uri, "type": "Type", "_label": "stale label"}],
        "identified_by": [
            {
                "type": "Name",
                "classified_as": [PRIMARY_NAME],
                "content": english,
                "language": [ENGLISH],
            },
            {
                "type": "Name",
                "classified_as": [PRIMARY_NAME],
                "content": french,
                "language": [FRENCH],
            },
        ],
        "produced_by": {
            "type": "Production",
            "timespan": {
                "type": "TimeSpan",
                "begin_of_the_begin": "1880-01-01T00:00:00Z",
                "end_of_the_end": "1880-12-31T23:59:59Z",
            },
        },
    }
    return assign_node_iris(data, uri)


def corpus_record(name: str, uri: str) -> dict[str, Any]:
    """A corpus record moved to ``uri``, with internal node IRIs assigned as the editor does."""
    data = load_record(CORPUS_DIR / name)
    data["id"] = uri
    return assign_node_iris(data, uri)


async def insert_record(
    engine: AsyncEngine,
    data: dict[str, Any],
    *,
    version: int = 1,
    status: RecordStatus = RecordStatus.ACTIVE,
    merged_into: UUID | None = None,
    updated_at: datetime | None = None,
) -> Record:
    """Insert a ``records`` row for ``data`` (its ``id`` is the URI); return it."""
    record = Record(
        id=id_of(data["id"]),
        uri=data["id"],
        type=data["type"],
        version=version,
        status=status,
        etag=record_etag(data),
        data=data,
        context_version=1,
        merged_into=merged_into,
    )
    if updated_at is not None:
        record.updated_at = updated_at
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(record)
        await session.commit()
    return record


async def insert_revision(
    engine: AsyncEngine,
    record_id: UUID,
    version: int,
    data: dict[str, Any],
    *,
    published_by: UUID | None = None,
    summary: str = "",
    authors: tuple[UUID, ...] = (),
    created_at: datetime | None = None,
) -> RecordRevision:
    """Insert a ``record_revisions`` row storing ``data`` as zstd JSON-LD; return it."""
    revision = RecordRevision(
        record_id=record_id,
        version=version,
        ntriples_zstd=compress(b""),
        jsonld_zstd=compress(orjson.dumps(data)),
        context_version=1,
        authors=list(authors),
        published_by=published_by or uuid7(),
        summary=summary,
    )
    if created_at is not None:
        revision.created_at = created_at
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(revision)
        await session.commit()
    return revision


async def insert_labels(
    engine: AsyncEngine, uri: str, labels: dict[str, str], default_label: str
) -> None:
    """Insert an ``entity_labels`` row for a ``Type``."""
    async with AsyncSession(engine) as session:
        session.add(EntityLabel(uri=uri, type="Type", labels=labels, default_label=default_label))
        await session.commit()


async def insert_draft(engine: AsyncEngine, record_id: UUID) -> None:
    """Insert a ``crdt_drafts`` row (an empty state) for a record."""
    async with AsyncSession(engine) as session:
        session.add(CrdtDraft(record_id=record_id, draft_epoch=0, base_version=0, state=b""))
        await session.commit()


async def auth_headers(engine: AsyncEngine, role: str, *scopes: str) -> dict[str, str]:
    """``Authorization`` header of a new user with ``role`` (token scoped to ``scopes``)."""
    user_id = await create_user(engine, role)
    token, _ = await create_token(engine, user_id, *scopes)
    return bearer(token)


def a_while_ago() -> datetime:
    """A fixed past instant with whole seconds (HTTP dates have no fractions)."""
    return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
