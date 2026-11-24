"""Rule membership and usage counts against a real QLever index (Step 2.1, Verify).

The module builds a small index with ``lae.bootstrap.index.build_index``: three records expanded
by ``lae.rdf.expand_to_ntriples`` in the default graph (a person who references a nationality
with its metatype inline, a local nationality concept, two objects of one type) and a sidecar
graph ``urn:graph:sidecar:aat`` with a broader hierarchy and sidecar-stated metatypes. It then
refreshes rule Value Sets and usage counts against it.

Needs the ``qlever`` command (qlever-control) and ``qlever-index``, and a free port named by
``LAE_TEST_QLEVER_PORT`` (your slot's QLever port, docs/agent-workflow.md); skipped otherwise.
CI has no QLever.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import asyncpg
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from lae.bootstrap.common import QLEVER_INDEX_WATERMARK, QLEVER_INDEX_WATERMARK_TIME, asyncpg_dsn
from lae.bootstrap.index import build_index, stop_command
from lae.db.models import Record
from lae.labels.upsert import upsert_entity_labels_for_record
from lae.projection import assign_node_iris
from lae.rdf import expand_to_ntriples
from lae.settings import Settings
from lae.sync.qlever_client import QLeverClient
from lae.vocab.membership import refresh_membership
from lae.vocab.usage import refresh_usage_counts
from tests.vocab.conftest import NATIONALITY, OCCUPATION, Labels
from tests.vocab.test_membership import define, members, rule

pytestmark = pytest.mark.integration

QLEVER_PORT_ENV = "LAE_TEST_QLEVER_PORT"
TOKEN = "vocab-test-token"
CONTEXT = "https://linked.art/ns/v1/linked-art.json"
BASE = "https://data.test/id/"
T = "https://aat.test/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
SKOS_BROADER = "http://www.w3.org/2004/02/skos/core#broader"
CRM = "http://www.cidoc-crm.org/cidoc-crm/"
SIDECAR = "urn:graph:sidecar:aat"


@dataclass(frozen=True)
class Concepts:
    """The URIs of the test data."""

    dutch: str = f"{T}dutch"  # metatype only inline in the person record
    french: str = f"{T}french"  # metatype stated in the sidecar graph
    excluded: str = f"{T}excluded"  # sidecar nationality, excluded by the rule
    artist: str = f"{T}artist"
    painting: str = f"{T}painting"
    oil: str = f"{T}oil-painting"
    root: str = f"{T}paintings-root"


C = Concepts()


@dataclass(frozen=True)
class Indexed:
    port: int
    person: dict[str, Any]
    local_nationality: str
    objects: list[str]

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"


def ref(uri: str, *metatypes: str) -> dict[str, Any]:
    reference: dict[str, Any] = {"id": uri, "type": "Type", "_label": uri.rsplit("/", 1)[-1]}
    if metatypes:
        reference["classified_as"] = [{"id": m, "type": "Type"} for m in metatypes]
    return reference


def record(type_name: str, label: str, **properties: Any) -> dict[str, Any]:
    uri = f"{BASE}{uuid7()}"
    data = {
        "@context": CONTEXT,
        "id": uri,
        "type": type_name,
        "_label": label,
        "identified_by": [{"type": "Name", "content": label}],
        **properties,
    }
    return assign_node_iris(data, uri)


def quad(subject: str, predicate: str, obj: str) -> str:
    return f"<{subject}> <{predicate}> <{obj}> <{SIDECAR}> ."


def sidecar_quads() -> list[str]:
    lines = []
    for concept in (C.dutch, C.french, C.excluded, C.artist, C.painting, C.oil, C.root):
        lines.append(quad(concept, RDF_TYPE, f"{CRM}E55_Type"))
    lines += [
        quad(C.french, f"{CRM}P2_has_type", NATIONALITY),
        quad(C.excluded, f"{CRM}P2_has_type", NATIONALITY),
        quad(C.oil, SKOS_BROADER, C.painting),
        quad(C.painting, f"{CRM}P127_has_broader_term", C.root),
    ]
    return lines


async def _sync_state(db_url: str, restore: dict[str, Any] | None = None) -> dict[str, Any]:
    keys = [QLEVER_INDEX_WATERMARK, QLEVER_INDEX_WATERMARK_TIME]
    connection = await asyncpg.connect(asyncpg_dsn(db_url))
    try:
        if restore is None:
            rows = await connection.fetch(
                "SELECT key, value::text FROM sync_state WHERE key = ANY($1::text[])", keys
            )
            return {row[0]: row[1] for row in rows}
        await connection.execute("DELETE FROM sync_state WHERE key = ANY($1::text[])", keys)
        for key, value in restore.items():
            await connection.execute(
                "INSERT INTO sync_state (key, value) VALUES ($1, $2::jsonb)", key, value
            )
        return restore
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def indexed(db_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Indexed]:
    """The test index, served on ``LAE_TEST_QLEVER_PORT``; stopped afterwards."""
    if shutil.which("qlever") is None or shutil.which("qlever-index") is None:
        pytest.skip("QLever (qlever-control and qlever-index) is not installed")
    raw_port = os.environ.get(QLEVER_PORT_ENV)
    if not raw_port:
        pytest.skip(f"{QLEVER_PORT_ENV} is not set (use your slot's QLever port)")
    port = int(raw_port)
    person = record(
        "Person", "Someone", classified_as=[ref(C.dutch, NATIONALITY), ref(C.artist, OCCUPATION)]
    )
    local = record("Type", "Frisian", classified_as=[ref(NATIONALITY)])
    objects = [
        record("HumanMadeObject", f"Thing {n}", classified_as=[ref(C.painting)]) for n in (1, 2)
    ]
    lines: list[str] = []
    for data in (person, local, *objects):
        lines += expand_to_ntriples(data, 1)
    lines += sidecar_quads()
    work = tmp_path_factory.mktemp("vocab-qlever")
    nt = work / "vocab.nq"
    nt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    name = f"lae_vocab_test_{port}"
    saved = asyncio.run(_sync_state(db_url))
    settings = Settings(database_url=db_url, qlever_access_token=SecretStr(TOKEN))
    try:
        build_index(
            nt, settings=settings, index_dir=work / "index", name=name, port=port, watermark=0
        )
        yield Indexed(port, person, local["id"], [o["id"] for o in objects])
    finally:
        subprocess.run(stop_command(name, port), cwd=work, capture_output=True, check=False)
        asyncio.run(_sync_state(db_url, saved))


@pytest.fixture
async def qlever(indexed: Indexed) -> AsyncIterator[QLeverClient]:
    async with QLeverClient(indexed.url, indexed.url, TOKEN) as client:
        yield client


@pytest.fixture
async def concept_rows(indexed: Indexed, db_engine: AsyncEngine, labels: Labels) -> Labels:
    """``entity_labels`` rows for the sidecar concepts and the published records."""
    for uri in (C.dutch, C.french, C.excluded, C.artist, C.painting, C.oil, C.root):
        await labels.put(uri, uri.rsplit("/", 1)[-1])
    async with AsyncSession(db_engine) as session:
        for data in (indexed.person, {"id": indexed.local_nationality, "type": "Type"}):
            labels.uris.append(data["id"])
        # Publishing the person harvests the Dutch metatype it states inline (ADR-0032).
        person = indexed.person
        await upsert_entity_labels_for_record(
            session, Record(id=uuid7(), uri=person["id"], type="Person", data=person), "en"
        )
        local = {
            "id": indexed.local_nationality,
            "type": "Type",
            "identified_by": [{"type": "Name", "content": "Frisian"}],
            "classified_as": [{"id": NATIONALITY, "type": "Type"}],
        }
        await upsert_entity_labels_for_record(
            session, Record(id=uuid7(), uri=local["id"], type="Type", data=local), "en"
        )
        await session.commit()
    return labels


async def test_rule_membership_joins_qlever_and_harvested_metatypes(
    db_engine: AsyncEngine,
    empty: None,
    indexed: Indexed,
    qlever: QLeverClient,
    concept_rows: Labels,
) -> None:
    dutch = await concept_rows.get(C.dutch)
    assert dutch is not None and dutch.metatype_uris == [NATIONALITY]
    await define(
        db_engine, "vocab:test_nationality", rule_=rule([NATIONALITY], exclusions=[C.excluded])
    )
    assert await members(db_engine, "vocab:test_nationality") == set()
    async with AsyncSession(db_engine) as session:
        count = await refresh_membership(session, "vocab:test_nationality", qlever=qlever)
        await session.commit()
    # QLever: the local concept record and the sidecar's French; entity_labels: the harvested
    # Dutch, which QLever cannot know (ADR-0021); the excluded concept is left out.
    expected = {indexed.local_nationality, C.french, C.dutch}
    assert await members(db_engine, "vocab:test_nationality") == expected and count == 3
    for uri, ids in ((C.dutch, ["vocab:test_nationality"]), (C.excluded, [])):
        row = await concept_rows.get(uri)
        assert row is not None and row.vocab_ids == ids

    # Without the harvest, QLever alone misses Dutch.
    rows = await qlever.query(f"SELECT ?t WHERE {{ <{C.dutch}> <{CRM}P2_has_type> ?t }}")
    assert rows == []


async def test_broader_roots_and_class_filters(
    db_engine: AsyncEngine, empty: None, qlever: QLeverClient, concept_rows: Labels
) -> None:
    await define(db_engine, "vocab:test_paintings", rule_=rule(roots=[C.root]))
    await define(
        db_engine, "vocab:test_materials", rule_=rule(roots=[C.root], classes=["Material"])
    )
    async with AsyncSession(db_engine) as session:
        assert await refresh_membership(session, "vocab:test_paintings", qlever=qlever) == 2
        assert await refresh_membership(session, "vocab:test_materials", qlever=qlever) == 0
        await session.commit()
    assert await members(db_engine, "vocab:test_paintings") == {C.painting, C.oil}


async def test_usage_counts_come_from_the_default_graph(
    db_engine: AsyncEngine, indexed: Indexed, qlever: QLeverClient, concept_rows: Labels
) -> None:
    async with AsyncSession(db_engine) as session:
        summary = await refresh_usage_counts(session, qlever)
        await session.commit()
    assert summary.classes["Type"] >= 3
    counts = {}
    for uri in (C.painting, C.dutch, C.artist, C.oil, C.root, C.french):
        row = await concept_rows.get(uri)
        assert row is not None
        counts[uri] = row.usage_count
    # Two objects use the painting type; the sidecar's broader links do not count.
    assert counts == {C.painting: 2, C.dutch: 1, C.artist: 1, C.oil: 0, C.root: 0, C.french: 0}
