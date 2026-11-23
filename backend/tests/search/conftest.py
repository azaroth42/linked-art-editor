"""Fixtures for the autocomplete tests: the app with a scripted QLever, and label rows.

``FakeQLever`` answers the application's QLever requests over ``httpx.MockTransport`` (no
network): the prefix search (recognised by ``ql:contains-word``) with ``search_rows``, anything
else (the context query) with ``context_rows``, both in ``application/qlever-results+json`` form.
It records every query. Tests that need a real QLever are in ``test_autocomplete_qlever.py``.
"""

from __future__ import annotations

import secrets
import string
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.app import create_app
from lae.bootstrap.common import QLEVER_INDEX_WATERMARK_TIME
from lae.db.models import EntityLabel, EntityLabelStatus, SyncState
from tests.auth.conftest import API_BASE_URL, make_settings

QLEVER_URL = "http://qlever.test:7000"
XSD = "http://www.w3.org/2001/XMLSchema#"
CRM = "http://www.cidoc-crm.org/cidoc-crm/"
AAT = "http://vocab.getty.edu/aat/"


def word() -> str:
    """A fresh lower-case word no other test uses (labels share one database)."""
    return "zq" + "".join(secrets.choice(string.ascii_lowercase) for _ in range(10))


def iri_cell(uri: str) -> str:
    return f"<{uri}>"


def int_cell(value: int) -> str:
    return f'"{value}"^^<{XSD}int>'


def date_cell(value: str) -> str:
    return f'"{value}"^^<{XSD}dateTime>'


def search_row(uri: str, klass: str, rank: int, label: str) -> list[str]:
    """One row of the prefix search (``?entity ?type ?rank ?label``)."""
    return [iri_cell(uri), iri_cell(f"{CRM}{klass}"), int_cell(rank), f'"{label}"']


@dataclass
class FakeQLever:
    """A scripted QLever query endpoint that records what it was asked."""

    search_rows: list[list[str | None]] = field(default_factory=list)
    context_rows: list[list[str | None]] = field(default_factory=list)
    fail: bool = False
    queries: list[str] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not str(request.url).startswith(QLEVER_URL):
            return httpx.Response(404)
        query = parse_qs(request.content.decode())["query"][0]
        self.queries.append(query)
        if self.fail:
            return httpx.Response(500, json={"status": "ERROR", "exception": "out of memory"})
        if "ql:contains-word" in query:
            selected, rows = ["?entity", "?type", "?rank", "?label"], self.search_rows
        else:
            selected = ["?entity", "?birth", "?death", "?parent", "?parentLabel"]
            rows = self.context_rows
        return httpx.Response(200, json={"status": "OK", "selected": selected, "res": rows})

    @property
    def searches(self) -> list[str]:
        return [query for query in self.queries if "ql:contains-word" in query]


@asynccontextmanager
async def serve(
    db_url: str, qlever: FakeQLever | None = None, **overrides: Any
) -> AsyncIterator[httpx.AsyncClient]:
    """The application, with ``qlever`` as its QLever (none configured if ``None``)."""
    fake = qlever or FakeQLever()
    settings = make_settings(
        db_url, qlever_url=QLEVER_URL if qlever is not None else None, **overrides
    )
    app = create_app(settings, http_transport=httpx.MockTransport(fake.handle))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
            yield client


async def put_label(
    engine: AsyncEngine,
    uri: str,
    type_name: str,
    default_label: str,
    *,
    labels: dict[str, str] | None = None,
    source: str = "local",
    status: EntityLabelStatus = EntityLabelStatus.PUBLISHED,
    updated_at: datetime | None = None,
) -> None:
    """Insert or replace one ``entity_labels`` row (``updated_at`` defaults to now)."""
    values: dict[str, Any] = {
        "type": type_name,
        "labels": labels if labels is not None else {"en": default_label},
        "default_label": default_label,
        "source": source,
        "status": status,
    }
    if updated_at is not None:
        values["updated_at"] = updated_at
    statement = insert(EntityLabel).values(uri=uri, **values)
    statement = statement.on_conflict_do_update(index_elements=[EntityLabel.uri], set_=values)
    async with AsyncSession(engine) as session:
        await session.execute(statement)
        await session.commit()


async def set_watermark_time(engine: AsyncEngine, value: Any) -> None:
    """Set ``sync_state.qlever_index_watermark_time`` (a JSON value); ``...`` removes it."""
    async with AsyncSession(engine) as session:
        await session.execute(delete(SyncState).where(SyncState.key == QLEVER_INDEX_WATERMARK_TIME))
        if value is not ...:
            session.add(SyncState(key=QLEVER_INDEX_WATERMARK_TIME, value=value))
        await session.commit()
