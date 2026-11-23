"""Fixtures for the vocabulary registry tests (Phase 2 Step 2.1).

``registry`` empties the vocabulary tables, imports ``profiles/vocab/`` (committed, so that the
application's own sessions see it) and empties them again afterwards; ``labels`` writes
``entity_labels`` rows with fresh URIs and deletes them afterwards. ``FakeQLever`` answers QLever
queries over ``httpx.MockTransport`` from a function of the query text.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.db.models import EntityLabel, EntityLabelStatus
from lae.sync.qlever_client import QLeverClient
from lae.vocab.loader import VOCAB_DIR, ImportSummary, load_from_yaml
from lae.vocab.service import clear_bundle_cache

AAT = "http://vocab.getty.edu/aat/"
NATIONALITY = f"{AAT}300379842"
GENDER = f"{AAT}300055147"
OCCUPATION = f"{AAT}300263369"
QLEVER_URL = "http://qlever.test:7000"
VOCAB_TABLES = "vocabulary_membership, slot_vocabulary_bindings, vocabulary_terms, vocabularies"


def fresh_uri(prefix: str = "https://data.test/concept/") -> str:
    """A URI no other test uses (all tests share one database)."""
    return prefix + secrets.token_hex(8)


async def empty_registry(engine: AsyncEngine) -> None:
    """Remove every Value Set, term, binding and membership row and every ``vocab_ids`` entry."""
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {VOCAB_TABLES}"))
        await connection.execute(
            text("UPDATE entity_labels SET vocab_ids = '{}' WHERE vocab_ids <> '{}'")
        )
    clear_bundle_cache()


async def import_seeds(engine: AsyncEngine) -> ImportSummary:
    """Import ``profiles/vocab/`` and commit."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        summary = await load_from_yaml(session, VOCAB_DIR)
        await session.commit()
    return summary


@pytest.fixture
async def registry(db_engine: AsyncEngine) -> AsyncIterator[ImportSummary]:
    """The seed Value Sets, committed; the registry is emptied before and after."""
    await empty_registry(db_engine)
    try:
        yield await import_seeds(db_engine)
    finally:
        await empty_registry(db_engine)


@pytest.fixture
async def empty(db_engine: AsyncEngine) -> AsyncIterator[None]:
    """An empty registry, emptied again afterwards."""
    await empty_registry(db_engine)
    try:
        yield
    finally:
        await empty_registry(db_engine)


@dataclass
class Labels:
    """Writes ``entity_labels`` rows and remembers them for cleanup."""

    engine: AsyncEngine
    uris: list[str] = field(default_factory=list)

    async def put(
        self,
        uri: str,
        default_label: str,
        *,
        type_name: str = "Type",
        labels: dict[str, str] | None = None,
        metatypes: list[str] | None = None,
        usage_count: int = 0,
        source: str = "aat",
        status: EntityLabelStatus = EntityLabelStatus.PUBLISHED,
    ) -> str:
        values: dict[str, Any] = {
            "type": type_name,
            "labels": labels if labels is not None else {"en": default_label},
            "default_label": default_label,
            "metatype_uris": metatypes or [],
            "usage_count": usage_count,
            "source": source,
            "status": status,
        }
        statement = insert(EntityLabel).values(uri=uri, **values)
        statement = statement.on_conflict_do_update(index_elements=[EntityLabel.uri], set_=values)
        async with AsyncSession(self.engine) as session:
            await session.execute(statement)
            await session.commit()
        self.uris.append(uri)
        return uri

    async def get(self, uri: str) -> EntityLabel | None:
        async with AsyncSession(self.engine) as session:
            return await session.get(EntityLabel, uri)

    async def cleanup(self) -> None:
        if self.uris:
            async with AsyncSession(self.engine) as session:
                await session.execute(delete(EntityLabel).where(EntityLabel.uri.in_(self.uris)))
                await session.commit()


@pytest.fixture
async def labels(db_engine: AsyncEngine) -> AsyncIterator[Labels]:
    """Write ``entity_labels`` rows that are deleted after the test."""
    written = Labels(db_engine)
    try:
        yield written
    finally:
        await written.cleanup()


@dataclass
class FakeQLever:
    """A scripted QLever query endpoint: ``answer(query)`` gives ``(variables, rows)``."""

    answer: Callable[[str], tuple[list[str], list[list[str | None]]]]
    queries: list[str] = field(default_factory=list)
    fail: bool = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.content.decode())["query"][0]
        self.queries.append(query)
        if self.fail:
            return httpx.Response(500, json={"status": "ERROR", "exception": "boom"})
        selected, rows = self.answer(query)
        return httpx.Response(200, json={"status": "OK", "selected": selected, "res": rows})

    def client(self) -> QLeverClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return QLeverClient(QLEVER_URL, http_client=http)


def iri_cell(uri: str) -> str:
    return f"<{uri}>"


def int_cell(value: int) -> str:
    return f'"{value}"^^<http://www.w3.org/2001/XMLSchema#int>'
