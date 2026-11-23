"""``refresh_usage_counts``: ``entity_labels.usage_count`` from QLever (Step 2.1)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.db.models import EntityLabel
from lae.sync.qlever_client import QLeverError
from lae.vocab.usage import DEFAULT_GRAPH, build_usage_query, refresh_usage_counts
from tests.vocab.conftest import FakeQLever, Labels, fresh_uri, int_cell, iri_cell

CRM = "http://www.cidoc-crm.org/cidoc-crm/"


def test_the_usage_query_counts_default_graph_triples_per_class() -> None:
    query = build_usage_query(f"{CRM}E55_Type")
    assert f"?o <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <{CRM}E55_Type>" in query
    assert f"GRAPH <{DEFAULT_GRAPH}> {{ ?s ?p ?o }}" in query
    assert query.endswith("GROUP BY ?o")


async def test_counts_are_written_and_the_rest_reset(
    db_engine: AsyncEngine, labels: Labels
) -> None:
    used = await labels.put(fresh_uri(), "used")
    two_classes = await labels.put(fresh_uri(), "both", type_name="Material")
    stale = await labels.put(fresh_uri(), "stale", usage_count=9)
    person = await labels.put(fresh_uri(), "someone", type_name="Person")
    before = await labels.get(used)

    def answer(query: str) -> tuple[list[str], list[list[str | None]]]:
        rows: list[list[str | None]] = []
        if f"<{CRM}E55_Type>" in query:
            rows = [[iri_cell(used), int_cell(7)], [iri_cell(two_classes), int_cell(3)]]
        elif f"<{CRM}E57_Material>" in query:
            rows = [[iri_cell(two_classes), int_cell(3)], [iri_cell(fresh_uri()), int_cell(1)]]
        elif f"<{CRM}E21_Person>" in query:
            rows = [[iri_cell(person), int_cell(2)]]
        return ["?o", "?n"], rows

    qlever = FakeQLever(answer)
    async with AsyncSession(db_engine) as session:
        summary = await refresh_usage_counts(session, qlever.client())
        await session.commit()
    assert summary.classes["Type"] == 2 and summary.classes["Person"] == 1
    assert summary.updated >= 4
    counts = {}
    async with AsyncSession(db_engine) as session:
        for uri in (used, two_classes, stale, person):
            counts[uri] = await session.scalar(
                select(EntityLabel.usage_count).where(EntityLabel.uri == uri)
            )
    assert counts == {used: 7, two_classes: 3, stale: 0, person: 2}
    after = await labels.get(used)
    assert before is not None and after is not None and after.updated_at == before.updated_at

    async with AsyncSession(db_engine) as session:
        again = await refresh_usage_counts(session, qlever.client())
        await session.commit()
    assert again.updated == 0


async def test_a_failing_qlever_changes_nothing(db_engine: AsyncEngine, labels: Labels) -> None:
    used = await labels.put(fresh_uri(), "used", usage_count=5)
    qlever = FakeQLever(lambda query: ([], []), fail=True)
    async with AsyncSession(db_engine) as session:
        with pytest.raises(QLeverError):
            await refresh_usage_counts(session, qlever.client())
    row = await labels.get(used)
    assert row is not None and row.usage_count == 5
