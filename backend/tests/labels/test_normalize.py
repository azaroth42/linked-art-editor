"""Tests for ``lae.labels.refs`` and ``lae.labels.normalize`` (decision A11, ADR-0006)."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lae.corpus import iter_corpus
from lae.db.models import EntityLabel, EntityLabelStatus
from lae.labels.normalize import known_reference_ids, known_uris, normalize_refs
from lae.labels.refs import (
    is_labelled_reference,
    iter_references,
    reference_ids,
    rewrite_references,
)
from tests.labels.conftest import MONA_LISA, corpus

PRIMARY_NAME = "http://vocab.getty.edu/aat/300404670"
OIL_PAINT = "http://vocab.getty.edu/aat/300015050"
LEONARDO = "http://vocab.getty.edu/ulan/500010879"
LOUVRE_SET = "http://data.louvre.example/all_holdings"
ENGLISH = "http://vocab.getty.edu/aat/300388277"
PAINTING = "http://vocab.getty.edu/aat/300033618"
TYPE_OF_WORK = "http://vocab.getty.edu/aat/300435443"
NODE = "https://example.org/id/1/node/0190f5a2-7b3c-7d4e-8f00-112233445566"

DOC: dict[str, Any] = {
    "@context": {"_label": "http://www.w3.org/2000/01/rdf-schema#label", "x": {"id": "urn:c"}},
    "id": "https://example.org/id/1",
    "type": "HumanMadeObject",
    "_label": "Root label stays",
    "classified_as": [
        {
            "id": "urn:known:1",
            "type": "Type",
            "_label": "Known type",
            "classified_as": [{"id": "urn:known:6", "type": "Type", "_label": "Metatype"}],
        }
    ],
    "identified_by": [
        {
            "type": "Name",
            "content": "A name",
            "_label": "Node label stays",
            "classified_as": [{"id": "urn:unknown:1", "type": "Type", "_label": "Pasted"}],
            "language": [
                {"id": "urn:known:2", "type": "Language", "_label": "En", "notation": "en"}
            ],
        }
    ],
    "member_of": [{"id": "urn:known:3"}, {"id": "urn:known:4", "_label": "Set"}],
    "produced_by": {
        "type": "Production",
        "_label": "Production label stays",
        "carried_out_by": [{"id": "urn:known:5", "type": "Person", "_label": "Artist"}],
        "timespan": {"id": NODE, "type": "TimeSpan", "_label": "Node with an IRI"},
    },
}
KNOWN = {f"urn:known:{n}" for n in range(1, 7)}


def test_a_reference_is_any_object_with_a_string_id() -> None:
    assert is_labelled_reference({"id": "urn:a"})
    assert is_labelled_reference({"id": "urn:a", "type": "Type", "_label": "A"})
    assert is_labelled_reference({"id": "urn:a", "type": "Language", "notation": "en"})
    assert not is_labelled_reference({"type": "Name", "content": "x"})
    assert not is_labelled_reference({"id": 1, "type": "Type"})
    assert not is_labelled_reference(["urn:a"])
    assert not is_labelled_reference("urn:a")


def test_iter_references_finds_references_below_the_root_only() -> None:
    ids = [reference["id"] for reference in iter_references(DOC)]
    assert ids == [
        "urn:known:1",
        "urn:known:6",
        "urn:unknown:1",
        "urn:known:2",
        "urn:known:3",
        "urn:known:4",
        "urn:known:5",
        NODE,
    ]
    # The root and @context are never references, even when they look like one.
    assert reference_ids({"id": "urn:root", "_label": "x"}) == set()
    assert reference_ids({"id": "urn:root", "@context": {"id": "urn:c"}}) == set()


def test_reference_ids_of_a_corpus_record() -> None:
    ids = reference_ids(corpus(MONA_LISA))
    assert {PRIMARY_NAME, OIL_PAINT, LEONARDO, LOUVRE_SET} <= ids
    # A Language with a notation and a Type with its metatype are references too.
    assert {ENGLISH, PAINTING, TYPE_OF_WORK} <= ids
    assert corpus(MONA_LISA)["id"] not in ids


def test_rewrite_references_copies_and_does_not_mutate() -> None:
    before = copy.deepcopy(DOC)

    def tag(reference: dict[str, Any]) -> dict[str, Any]:
        reference["seen"] = True
        return reference

    result = rewrite_references(DOC, tag)
    assert DOC == before
    rewritten = sorted(r["id"] for r in _walk_dicts(result) if r.get("seen"))
    assert rewritten == sorted(r["id"] for r in iter_references(DOC))
    assert result["@context"] == DOC["@context"] and result["@context"] is not DOC["@context"]


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(_walk_dicts(child))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_dicts(item))
    return found


def test_normalize_drops_label_only_on_known_references() -> None:
    result = normalize_refs(DOC, KNOWN)
    metatype = {"id": "urn:known:6", "type": "Type"}
    assert result["classified_as"] == [
        {"id": "urn:known:1", "type": "Type", "classified_as": [metatype]}
    ]
    assert result["member_of"] == [{"id": "urn:known:3"}, {"id": "urn:known:4"}]
    production = result["produced_by"]
    assert production["carried_out_by"] == [{"id": "urn:known:5", "type": "Person"}]
    name = result["identified_by"][0]
    assert name["language"] == [{"id": "urn:known:2", "type": "Language", "notation": "en"}]
    # Unknown target: the pasted label is the only one there is, so it stays (ADR-0006).
    assert name["classified_as"] == [{"id": "urn:unknown:1", "type": "Type", "_label": "Pasted"}]
    # Embedded nodes (with or without an internal IRI), the root and @context keep their _label.
    assert name["_label"] == "Node label stays"
    assert production["_label"] == "Production label stays"
    assert production["timespan"]["_label"] == "Node with an IRI"
    assert result["_label"] == "Root label stays"
    assert result["@context"] == DOC["@context"]


def test_normalize_with_nothing_known_changes_nothing() -> None:
    assert normalize_refs(DOC, set()) == DOC


def test_normalize_does_not_mutate_its_input() -> None:
    before = copy.deepcopy(DOC)
    normalize_refs(DOC, reference_ids(DOC))
    assert DOC == before


def test_normalize_whole_corpus() -> None:
    for path, data in iter_corpus():
        ids = reference_ids(data)
        stripped = normalize_refs(data, ids)
        assert all("_label" not in reference for reference in iter_references(stripped)), path
        assert normalize_refs(data, set()) == data, path
        # Only reference labels change: everything else is identical.
        assert normalize_refs(stripped, ids) == stripped, path
        assert _without_reference_labels(data) == stripped, path


def _without_reference_labels(data: dict[str, Any]) -> dict[str, Any]:
    def drop(reference: dict[str, Any]) -> dict[str, Any]:
        reference.pop("_label", None)
        return reference

    return rewrite_references(data, drop)


# --- the one query ---------------------------------------------------------------------------


async def test_known_reference_ids_is_one_query(
    db_session: AsyncSession, label_queries: list[str]
) -> None:
    db_session.add_all(
        [
            EntityLabel(uri=PRIMARY_NAME, type="Type", default_label="Primary Name", source="aat"),
            EntityLabel(uri=OIL_PAINT, type="Material", default_label="oil paint", source="aat"),
            EntityLabel(
                uri=LEONARDO,
                type="Person",
                default_label="Leonardo",
                status=EntityLabelStatus.DRAFT,
            ),
            EntityLabel(uri="urn:not-referenced", type="Type", default_label="x"),
        ]
    )
    await db_session.flush()
    label_queries.clear()
    known = await known_reference_ids(db_session, corpus(MONA_LISA))
    assert known == {PRIMARY_NAME, OIL_PAINT, LEONARDO}
    assert len(label_queries) == 1
    assert "ANY" in label_queries[0].upper()


async def test_known_uris_without_uris_runs_no_query(
    db_session: AsyncSession, label_queries: list[str]
) -> None:
    assert await known_uris(db_session, set()) == set()
    assert await known_reference_ids(db_session, {"id": "urn:root", "_label": "x"}) == set()
    assert label_queries == []


async def test_normalize_a_corpus_record_against_entity_labels(db_session: AsyncSession) -> None:
    db_session.add(EntityLabel(uri=PRIMARY_NAME, type="Type", default_label="Primary Name"))
    await db_session.flush()
    data = corpus(MONA_LISA)
    stored = normalize_refs(data, await known_reference_ids(db_session, data))
    for reference in iter_references(stored):
        assert ("_label" in reference) == (reference["id"] != PRIMARY_NAME)
