"""``refresh_membership``: enumerated and rule Value Sets, ``vocab_ids`` (Step 2.1, ADR-0032).

QLever is scripted here (``FakeQLever``); ``test_qlever.py`` runs the same against a real index.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.db.models import Enforcement, Vocabulary, VocabularyMembership, VocabularyMode
from lae.sync.qlever_client import QLeverError
from lae.vocab.loader import TermDef, ValueSetDef, _write_value_set
from lae.vocab.membership import (
    UnknownVocabularyError,
    build_membership_query,
    concept_class_iris,
    refresh_membership,
)
from tests.vocab.conftest import NATIONALITY, OCCUPATION, FakeQLever, Labels, fresh_uri, iri_cell

CRM = "http://www.cidoc-crm.org/cidoc-crm/"


def rule(
    metatypes: list[str] | None = None,
    roots: list[str] | None = None,
    exclusions: list[str] | None = None,
    classes: list[str] | None = None,
) -> dict[str, list[str]]:
    return {
        "metatype_uris": metatypes or [],
        "broader_roots": roots or [],
        "exclusions": exclusions or [],
        "classes": classes or [],
    }


async def define(
    engine: AsyncEngine,
    vocabulary_id: str,
    *,
    terms: tuple[str, ...] = (),
    rule_: dict[str, list[str]] | None = None,
) -> None:
    """Create or replace a Value Set and commit."""
    value_set = ValueSetDef(
        id=vocabulary_id,
        version=1,
        label={"en": vocabulary_id},
        mode=VocabularyMode.RULE if rule_ is not None else VocabularyMode.ENUMERATED,
        enforcement=Enforcement.EXTENSIBLE,
        rule=rule_,
        terms=tuple(TermDef(uri=uri) for uri in terms),
    )
    async with AsyncSession(engine) as session:
        await _write_value_set(session, value_set, None)
        await session.commit()


async def refresh(engine: AsyncEngine, vocabulary_id: str, **kwargs: Any) -> int:
    async with AsyncSession(engine) as session:
        count = await refresh_membership(session, vocabulary_id, **kwargs)
        await session.commit()
    return count


async def members(engine: AsyncEngine, vocabulary_id: str) -> set[str]:
    async with AsyncSession(engine) as session:
        return set(
            await session.scalars(
                select(VocabularyMembership.uri).where(
                    VocabularyMembership.vocabulary_id == vocabulary_id
                )
            )
        )


def test_the_membership_query() -> None:
    classes = concept_class_iris(1)
    assert classes["Type"] == f"{CRM}E55_Type" and classes["Material"] == f"{CRM}E57_Material"
    query = build_membership_query(rule([NATIONALITY], ["http://r.test/1"]), classes)
    assert f"VALUES ?metatype {{ <{NATIONALITY}> }} ?concept crm:P2_has_type ?metatype" in query
    assert "?concept (skos:broader|crm:P127_has_broader_term)+ ?root" in query
    assert "UNION" in query and query.count(f"<{CRM}E") == 5
    only_classes = build_membership_query(rule(classes=["Material"]), classes)
    assert "UNION" not in only_classes and "?metatype" not in only_classes
    assert f"VALUES ?class {{ <{CRM}E57_Material> }}" in only_classes
    with pytest.raises(ValueError, match="Not a valid IRI"):
        build_membership_query(rule(["http://x.test/> } DROP ALL { <a"]), classes)


async def test_enumerated_members_are_its_terms_and_reach_vocab_ids(
    db_engine: AsyncEngine, empty: None, labels: Labels
) -> None:
    one, two = await labels.put(fresh_uri(), "one"), await labels.put(fresh_uri(), "two")
    three = fresh_uri()
    await define(db_engine, "vocab:test_enum", terms=(one, two, three))
    assert await refresh(db_engine, "vocab:test_enum") == 3
    assert await members(db_engine, "vocab:test_enum") == {one, two, three}
    for uri in (one, two):
        row = await labels.get(uri)
        assert row is not None and row.vocab_ids == ["vocab:test_enum"]

    await define(db_engine, "vocab:test_enum", terms=(two,))
    await define(db_engine, "vocab:other", terms=(two,))
    await refresh(db_engine, "vocab:other")
    assert await refresh(db_engine, "vocab:test_enum") == 1
    row_one, row_two = await labels.get(one), await labels.get(two)
    assert row_one is not None and row_one.vocab_ids == []
    assert row_two is not None and row_two.vocab_ids == ["vocab:other", "vocab:test_enum"]


async def test_a_rule_without_qlever_uses_entity_labels_and_pinned_terms(
    db_engine: AsyncEngine, empty: None, labels: Labels
) -> None:
    dutch = await labels.put(fresh_uri(), "Dutch", metatypes=[NATIONALITY])
    excluded = await labels.put(fresh_uri(), "Old", metatypes=[NATIONALITY])
    artist = await labels.put(fresh_uri(), "artist", metatypes=[OCCUPATION])
    material = await labels.put(fresh_uri(), "oak", type_name="Material", metatypes=[NATIONALITY])
    person = await labels.put(fresh_uri(), "Someone", type_name="Person", metatypes=[NATIONALITY])
    pinned = fresh_uri()
    await define(
        db_engine,
        "vocab:test_nat",
        terms=(pinned,),
        rule_=rule([NATIONALITY], exclusions=[excluded]),
    )
    assert await refresh(db_engine, "vocab:test_nat") == 3
    assert await members(db_engine, "vocab:test_nat") == {dutch, material, pinned}
    row = await labels.get(dutch)
    assert row is not None and row.vocab_ids == ["vocab:test_nat"]
    for other in (excluded, artist, person):
        found = await labels.get(other)
        assert found is not None and found.vocab_ids == []

    await define(
        db_engine, "vocab:test_nat", rule_=rule([NATIONALITY], classes=["Type"], exclusions=[])
    )
    assert await members(db_engine, "vocab:test_nat") == {dutch, material, pinned}  # not yet
    await refresh(db_engine, "vocab:test_nat")
    assert await members(db_engine, "vocab:test_nat") == {dutch, excluded}

    await define(db_engine, "vocab:test_materials", rule_=rule(classes=["Material"]))
    await refresh(db_engine, "vocab:test_materials")
    assert material in await members(db_engine, "vocab:test_materials")
    assert dutch not in await members(db_engine, "vocab:test_materials")


async def test_a_rule_with_qlever_adds_what_qlever_finds(
    db_engine: AsyncEngine, empty: None, labels: Labels
) -> None:
    harvested = await labels.put(fresh_uri(), "Dutch", metatypes=[NATIONALITY])
    from_qlever, excluded = fresh_uri(), fresh_uri()
    root = fresh_uri()
    qlever = FakeQLever(
        lambda query: (["?concept"], [[iri_cell(from_qlever)], [iri_cell(excluded)], [None]])
    )
    await define(
        db_engine,
        "vocab:test_q",
        rule_=rule([NATIONALITY], roots=[root], exclusions=[excluded]),
    )
    count = await refresh(db_engine, "vocab:test_q", qlever=qlever.client())
    assert count == 2
    assert await members(db_engine, "vocab:test_q") == {harvested, from_qlever}
    [query] = qlever.queries
    assert f"<{root}>" in query and f"<{NATIONALITY}>" in query

    # Roots only: entity_labels cannot follow broader, so only QLever answers.
    await define(db_engine, "vocab:test_q", rule_=rule(roots=[root]))
    assert await refresh(db_engine, "vocab:test_q", qlever=qlever.client()) == 2
    assert await members(db_engine, "vocab:test_q") == {from_qlever, excluded}


async def test_a_failing_qlever_writes_nothing(
    db_engine: AsyncEngine, empty: None, labels: Labels
) -> None:
    dutch = await labels.put(fresh_uri(), "Dutch", metatypes=[NATIONALITY])
    await define(db_engine, "vocab:test_fail", rule_=rule([NATIONALITY]))
    await refresh(db_engine, "vocab:test_fail")
    qlever = FakeQLever(lambda query: ([], []), fail=True)
    async with AsyncSession(db_engine) as session:
        with pytest.raises(QLeverError):
            await refresh_membership(session, "vocab:test_fail", qlever=qlever.client())
    assert await members(db_engine, "vocab:test_fail") == {dutch}


async def test_an_unknown_value_set(db_engine: AsyncEngine, empty: None) -> None:
    async with AsyncSession(db_engine) as session:
        with pytest.raises(UnknownVocabularyError):
            await refresh_membership(session, "vocab:nowhere")
        assert await session.get(Vocabulary, "vocab:nowhere") is None
