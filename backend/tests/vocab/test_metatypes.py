"""Harvesting inline metatypes into ``entity_labels.metatype_uris`` (Step 2.1, ADR-0032)."""

from __future__ import annotations

from typing import Any

import asyncpg
import orjson
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from lae.bootstrap.common import asyncpg_dsn
from lae.corpus import iter_corpus
from lae.db.models import Record, RecordStatus
from lae.labels.upsert import upsert_entity_labels_for_record
from lae.vocab.metatypes import (
    CONCEPT_PATH,
    METATYPE_PATH,
    harvest_all_metatypes,
    harvest_record_metatypes,
    inline_metatypes,
)
from tests.vocab.conftest import AAT, NATIONALITY, OCCUPATION, Labels, fresh_uri

BRIEF_TEXT = f"{AAT}300418049"

PAIRS_SQL = f"""
SELECT concept ->> 'id', metatype #>> '{{}}'
FROM jsonb_path_query($1::jsonb, '{CONCEPT_PATH}', '{{}}', true) AS concept
CROSS JOIN LATERAL jsonb_path_query(concept, '{METATYPE_PATH}', '{{}}', true) AS metatype
"""


def concept(uri: str, *metatypes: str, type_name: str = "Type") -> dict[str, Any]:
    """A concept reference with inline metatypes."""
    return {
        "id": uri,
        "type": type_name,
        "_label": "x",
        "classified_as": [{"id": m, "type": "Type"} for m in metatypes],
    }


def person_with(*classifications: dict[str, Any], uri: str | None = None) -> dict[str, Any]:
    return {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": uri or fresh_uri("https://data.test/id/"),
        "type": "Person",
        "classified_as": list(classifications),
    }


def pairs(found: dict[str, set[str]]) -> set[tuple[str, str]]:
    return {(uri, metatype) for uri, metatypes in found.items() for metatype in metatypes}


def test_inline_metatypes_of_concept_references() -> None:
    dutch, artist, oil = (fresh_uri() for _ in range(3))
    record = person_with(concept(dutch, NATIONALITY), concept(artist, OCCUPATION))
    record["referred_to_by"] = [
        {
            "type": "LinguisticObject",
            "content": "...",
            "classified_as": [concept(f"{AAT}300435416", BRIEF_TEXT)],
        }
    ]
    record["carried_out"] = [
        {"type": "Activity", "used_specific_object": [concept(oil, "urn:m", type_name="Material")]}
    ]
    record["member_of"] = [
        # Not concepts: a classification of a Group or Person is not a metatype.
        {"id": fresh_uri(), "type": "Group", "classified_as": [{"id": "urn:no"}]},
        # A single object instead of an array; an entry without an id; no id on the concept.
        {"type": "Type", "classified_as": [{"id": "urn:no"}]},
    ]
    record["equivalent"] = [{"id": "urn:eq", "type": "Type", "classified_as": {"id": "urn:one"}}]
    record["classified_as"].append({"id": "urn:c", "type": "Type", "classified_as": [{"x": 1}]})
    assert inline_metatypes(record) == {
        dutch: {NATIONALITY},
        artist: {OCCUPATION},
        f"{AAT}300435416": {BRIEF_TEXT},
        oil: {"urn:m"},
        "urn:eq": {"urn:one"},
    }


def test_a_concept_record_states_its_own_metatypes() -> None:
    uri = fresh_uri()
    record = {"id": uri, "type": "Type", "classified_as": [{"id": NATIONALITY, "type": "Type"}]}
    assert inline_metatypes(record) == {uri: {NATIONALITY}}
    assert inline_metatypes({"id": uri, "type": "Person", "classified_as": [{"id": "x"}]}) == {}


async def test_the_sql_harvest_selects_what_the_python_one_does_over_the_corpus(
    db_url: str,
) -> None:
    connection = await asyncpg.connect(asyncpg_dsn(db_url))
    try:
        total = 0
        for path, record in iter_corpus():
            rows = await connection.fetch(PAIRS_SQL, orjson.dumps(record).decode())
            assert {(r[0], r[1]) for r in rows} == pairs(inline_metatypes(record)), path.name
            total += len(rows)
    finally:
        await connection.close()
    assert total > 150  # docs/questions.md, Step 1.3: 183 inline metatypes in the corpus


async def test_harvest_adds_to_existing_rows_only(db_engine: AsyncEngine, labels: Labels) -> None:
    dutch = await labels.put(fresh_uri(), "Dutch", metatypes=["urn:already"])
    artist = await labels.put(fresh_uri(), "artist", metatypes=[OCCUPATION])
    unknown = fresh_uri()
    before = await labels.get(dutch)
    record = person_with(
        concept(dutch, NATIONALITY), concept(artist, OCCUPATION), concept(unknown, NATIONALITY)
    )
    async with AsyncSession(db_engine) as session:
        assert await harvest_record_metatypes(session, record) == 1
        assert await harvest_record_metatypes(session, record) == 0
        await session.commit()
    after = await labels.get(dutch)
    assert before is not None and after is not None
    assert after.metatype_uris == sorted([NATIONALITY, "urn:already"])
    assert after.updated_at == before.updated_at
    assert (await labels.get(unknown)) is None
    # Union only: a record that no longer repeats a metatype does not remove it.
    async with AsyncSession(db_engine) as session:
        assert await harvest_record_metatypes(session, person_with(concept(dutch))) == 0
    again = await labels.get(dutch)
    assert again is not None and NATIONALITY in again.metatype_uris


async def test_publishing_a_record_harvests_its_metatypes(
    db_engine: AsyncEngine, labels: Labels
) -> None:
    dutch = await labels.put(fresh_uri(), "Dutch")
    record_uri = fresh_uri("https://data.test/id/")
    labels.uris.append(record_uri)
    data = person_with(concept(dutch, NATIONALITY), uri=record_uri)
    record = Record(id=uuid7(), uri=record_uri, type="Person", data=data)
    async with AsyncSession(db_engine) as session:
        await upsert_entity_labels_for_record(session, record)
        await session.commit()
    row = await labels.get(dutch)
    assert row is not None and row.metatype_uris == [NATIONALITY]

    # A local concept record: its own classified_as is its metatype, on its own row.
    concept_uri = fresh_uri("https://data.test/id/")
    labels.uris.append(concept_uri)
    own = {"id": concept_uri, "type": "Type", "classified_as": [{"id": OCCUPATION}]}
    async with AsyncSession(db_engine) as session:
        await upsert_entity_labels_for_record(
            session, Record(id=uuid7(), uri=concept_uri, type="Type", data=own)
        )
        await session.commit()
    own_row = await labels.get(concept_uri)
    assert own_row is not None and own_row.metatype_uris == [OCCUPATION]


async def test_harvest_all_reads_every_active_record(
    db_engine: AsyncEngine, db_url: str, labels: Labels
) -> None:
    dutch = await labels.put(fresh_uri(), "Dutch")
    french = await labels.put(fresh_uri(), "French")
    active = person_with(concept(dutch, NATIONALITY))
    deleted = person_with(concept(french, NATIONALITY))
    rows = [(active, RecordStatus.ACTIVE), (deleted, RecordStatus.DELETED)]
    ids = []
    async with AsyncSession(db_engine) as session:
        for data, status in rows:
            record = Record(
                id=uuid7(),
                uri=data["id"],
                type="Person",
                status=status,
                etag="x",
                data=data,
                context_version=1,
            )
            session.add(record)
            ids.append(record.id)
        await session.commit()
    connection = await asyncpg.connect(asyncpg_dsn(db_url))
    try:
        assert await harvest_all_metatypes(connection) >= 1
        assert await harvest_all_metatypes(connection) == 0
    finally:
        await connection.execute("DELETE FROM records WHERE id = ANY($1::uuid[])", ids)
        await connection.close()
    dutch_row, french_row = await labels.get(dutch), await labels.get(french)
    assert dutch_row is not None and dutch_row.metatype_uris == [NATIONALITY]
    assert french_row is not None and french_row.metatype_uris == []
