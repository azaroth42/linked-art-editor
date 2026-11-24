"""Tests for ``lae.labels.transclude``: locale-aware read-time ``_label`` transclusion."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lae.corpus import iter_corpus
from lae.db.models import EntityLabel
from lae.labels.extract import labels_from_record
from lae.labels.normalize import known_reference_ids, normalize_refs
from lae.labels.refs import iter_references, rewrite_references
from lae.labels.transclude import hydrate_labels, label_language, match_label, resolve_label
from lae.labels.upsert import upsert_entity_labels_for_record
from tests.labels.conftest import (
    HACHA,
    MONA_LISA,
    NIGHT_WATCH,
    SPANISH_ONLY,
    YUAG_OBJECT,
    as_record,
    corpus,
)

PRIMARY_NAME = "http://vocab.getty.edu/aat/300404670"
PASTED = "https://example.net/authority/42"

# --- match_label: RFC 4647 basic filtering ---------------------------------------------------

LABELS = {"en": "colour", "en-US": "color", "fr": "couleur", "zh-Hant": "顏色", "und": "?"}


@pytest.mark.parametrize(
    ("chain", "expected"),
    [
        (["fr", "en"], "couleur"),
        (["de", "fr", "en"], "couleur"),
        (["en"], "colour"),
        (["en-US", "en"], "color"),
        (["EN-us"], "color"),
        (["en-GB", "en"], "colour"),
        (["zh", "en"], "顏色"),
        (["zh-Hant-TW", "en"], "colour"),
        (["de"], None),
        ([], None),
    ],
)
def test_match_label_basic_filtering(chain: list[str], expected: str | None) -> None:
    assert match_label(LABELS, chain) == expected


def test_a_range_matches_longer_tags_deterministically() -> None:
    assert match_label({"en-US": "color", "en-GB": "colour"}, ["en"]) == "colour"
    assert match_label({"en-GB": "colour", "en": "plain"}, ["en"]) == "plain"
    # A range is a prefix only at a subtag boundary.
    assert match_label({"enm": "Middle English"}, ["en"]) is None


def test_resolve_label_falls_back_to_the_default() -> None:
    assert resolve_label(LABELS, "default", ["it"]) == "default"
    assert resolve_label({}, "default", ["en"]) == "default"
    assert resolve_label(LABELS, "default", ["fr", "en"]) == "couleur"


def test_label_language() -> None:
    labels = {"en": "Mona Lisa", "fr": "La Joconde", "und": "Gioconda", "EN-gb": "Mona Lisa"}
    assert label_language(labels, "La Joconde") == "fr"
    assert label_language(labels, "Mona Lisa") == "en"  # first in tag order
    assert label_language(labels, "Gioconda") is None  # undetermined is not a language
    assert label_language(labels, "something else") is None


# --- hydrate_labels against entity_labels ----------------------------------------------------

RECORDS = (MONA_LISA, NIGHT_WATCH, HACHA, SPANISH_ONLY, YUAG_OBJECT)


def gallery() -> dict[str, Any]:
    """A text about five corpus records, named with AAT Primary Name, citing a pasted URI."""
    objects = [
        {"id": corpus(name)["id"], "type": "HumanMadeObject", "_label": "stale"} for name in RECORDS
    ]
    return {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": "https://example.org/id/gallery",
        "type": "LinguisticObject",
        "_label": "Gallery",
        "identified_by": [
            {
                "type": "Name",
                "content": "Gallery",
                "classified_as": [{"id": PRIMARY_NAME, "type": "Type", "_label": "stale"}],
            }
        ],
        "about": [
            *objects,
            {"id": PASTED, "type": "Person", "_label": "Pasted label"},
            {"id": PASTED + "/x"},
        ],
    }


async def _load_labels(session: AsyncSession) -> None:
    for name in RECORDS:
        await upsert_entity_labels_for_record(session, as_record(corpus(name)), "en")
    session.add(
        EntityLabel(
            uri=PRIMARY_NAME,
            type="Type",
            labels={"en": "primary name", "fr": "nom principal", "nl": "voorkeursnaam"},
            default_label="primary name",
            source="aat",
        )
    )
    await session.flush()


def labels_by_id(data: dict[str, Any]) -> dict[str, str | None]:
    return {reference["id"]: reference.get("_label") for reference in iter_references(data)}


async def stored_gallery(session: AsyncSession) -> dict[str, Any]:
    await _load_labels(session)
    data = gallery()
    return normalize_refs(data, await known_reference_ids(session, data))


async def test_normalized_document_keeps_only_the_unresolved_label(
    db_session: AsyncSession,
) -> None:
    stored = await stored_gallery(db_session)
    assert {uri: label for uri, label in labels_by_id(stored).items() if label} == {
        PASTED: "Pasted label"
    }


@pytest.mark.parametrize(
    ("chain", "expected"),
    [
        (
            ["fr", "en"],
            ["La Joconde", "The Night Watch", "Hacha (Ceremonial Axe)", "Hacha",
             "Female Sensibility", "nom principal"],
        ),
        (
            ["nl", "en"],
            ["Mona Lisa", "De Nachtwacht", "Hacha (Ceremonial Axe)", "Hacha",
             "Female Sensibility", "voorkeursnaam"],
        ),
        (
            ["it", "es", "en"],
            ["La Gioconda", "The Night Watch", "Hacha (Ceremonial Axe)", "Hacha",
             "Female Sensibility", "primary name"],
        ),
        (
            ["de"],
            ["Mona Lisa", "The Night Watch", "Hacha (Ceremonial Axe)", "Hacha",
             "Female Sensibility", "primary name"],
        ),
    ],
)  # fmt: skip
async def test_hydrate_labels_in_the_callers_languages(
    db_session: AsyncSession, chain: list[str], expected: list[str]
) -> None:
    stored = await stored_gallery(db_session)
    hydrated = await hydrate_labels(stored, chain, db_session)
    labels = labels_by_id(hydrated)
    ids = [corpus(name)["id"] for name in RECORDS] + [PRIMARY_NAME]
    assert [labels[uri] for uri in ids] == expected
    # Unresolved references keep what they had (a label, or none).
    assert labels[PASTED] == "Pasted label"
    assert labels[PASTED + "/x"] is None
    # Nothing but reference labels changed.
    assert _without_reference_labels(hydrated) == _without_reference_labels(stored)
    assert hydrated["_label"] == "Gallery"


async def test_hydrate_labels_reports_label_languages(db_session: AsyncSession) -> None:
    stored = await stored_gallery(db_session)
    languages: dict[str, str] = {}
    hydrated = await hydrate_labels(stored, ["fr", "en"], db_session, languages=languages)
    labels = labels_by_id(hydrated)
    mona_lisa = corpus(MONA_LISA)["id"]
    assert (labels[mona_lisa], languages[mona_lisa]) == ("La Joconde", "fr")
    assert languages[corpus(NIGHT_WATCH)["id"]] == "en"
    assert set(languages) <= {uri for uri, label in labels.items() if label}
    assert PASTED not in languages
    assert await hydrate_labels(stored, ["fr", "en"], db_session) == hydrated


async def test_hydrate_labels_replaces_stale_labels(db_session: AsyncSession) -> None:
    await _load_labels(db_session)
    hydrated = await hydrate_labels(gallery(), ["en"], db_session)
    assert labels_by_id(hydrated)[corpus(MONA_LISA)["id"]] == "Mona Lisa"


async def test_hydrate_labels_is_one_query(
    db_session: AsyncSession, label_queries: list[str]
) -> None:
    stored = await stored_gallery(db_session)
    label_queries.clear()
    await hydrate_labels(stored, ["fr", "en"], db_session)
    assert len(label_queries) == 1


async def test_hydrate_labels_without_references_runs_no_query(
    db_session: AsyncSession, label_queries: list[str]
) -> None:
    data = {"id": "https://example.org/id/1", "type": "Person", "_label": "Solo"}
    assert await hydrate_labels(data, ["en"], db_session) == data
    assert label_queries == []


async def test_hydrate_labels_does_not_mutate_its_input(db_session: AsyncSession) -> None:
    stored = await stored_gallery(db_session)
    before = normalize_refs(stored, set())
    await hydrate_labels(stored, ["fr", "en"], db_session)
    assert stored == before


async def test_whole_corpus_round_trip(db_session: AsyncSession) -> None:
    """Publish labels for every corpus record, then normalize and transclude every record."""
    records = dict((data["id"], data) for _, data in iter_corpus())
    for data in records.values():
        await upsert_entity_labels_for_record(db_session, as_record(data), "en")
    await db_session.flush()
    resolved = 0
    for path, data in iter_corpus():
        stored = normalize_refs(data, await known_reference_ids(db_session, data))
        hydrated = await hydrate_labels(stored, ["fr", "en"], db_session)
        assert _without_reference_labels(hydrated) == _without_reference_labels(data), path
        for original, reference in zip(
            iter_references(data), iter_references(hydrated), strict=True
        ):
            target = records.get(reference["id"])
            if target is None:
                assert reference.get("_label") == original.get("_label"), path
            else:
                labels, default = labels_from_record(target, "en")
                assert reference["_label"] == labels.get("fr", labels.get("en", default)), path
                resolved += 1
    assert resolved >= 5


def _without_reference_labels(data: dict[str, Any]) -> dict[str, Any]:
    def drop(reference: dict[str, Any]) -> dict[str, Any]:
        reference.pop("_label", None)
        return reference

    return rewrite_references(data, drop)
