"""Tests for ``lae.labels.langtags``."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from lae.corpus import iter_corpus
from lae.labels.langtags import (
    AAT_LANGUAGE_TAGS,
    ISO_639_2_TO_1,
    UNDETERMINED,
    aat_id,
    aat_language_tag,
    language_tag,
    normalize_tag,
    primary_language,
)

ENGLISH = "http://vocab.getty.edu/aat/300388277"
SPANISH = "http://vocab.getty.edu/aat/300389311"


def _languages(value: Any) -> Iterator[dict[str, Any]]:
    """Yield every object that is an entry of a ``language`` property, at any depth."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "language" and isinstance(child, list):
                yield from (entry for entry in child if isinstance(entry, dict))
            yield from _languages(child)
    elif isinstance(value, list):
        for item in value:
            yield from _languages(item)


def test_the_map_covers_about_forty_common_languages() -> None:
    assert len(AAT_LANGUAGE_TAGS) >= 40
    assert len(set(AAT_LANGUAGE_TAGS.values())) == len(AAT_LANGUAGE_TAGS)
    for concept, tag in AAT_LANGUAGE_TAGS.items():
        assert concept.isdigit() and concept.startswith("300")
        assert normalize_tag(tag) == tag
        assert len(tag) == 2


def test_iso_639_2_aliases_point_into_the_map() -> None:
    tags = set(AAT_LANGUAGE_TAGS.values())
    for code, tag in ISO_639_2_TO_1.items():
        assert len(code) == 3 and code.isalpha() and code.islower()
        assert tag in tags
    assert set(ISO_639_2_TO_1.values()) == tags


def test_corpus_notations_agree_with_the_map() -> None:
    checked = 0
    for _, record in iter_corpus():
        for language in _languages(record):
            tag = aat_language_tag(language.get("id", ""))
            if "notation" in language and tag is not None:
                assert normalize_tag(language["notation"]) == tag, language
                checked += 1
    assert checked >= 10


def test_every_aat_language_in_the_corpus_is_mapped() -> None:
    aat_languages = {
        language["id"]
        for _, record in iter_corpus()
        for language in _languages(record)
        if aat_id(language.get("id", "")) is not None
    }
    assert aat_languages
    assert {uri for uri in aat_languages if aat_language_tag(uri) is None} == set()


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("en", "en"),
        ("EN", "en"),
        (" fr ", "fr"),
        ("en-gb", "en-GB"),
        ("EN-GB", "en-GB"),
        ("zh-hant-tw", "zh-Hant-TW"),
        ("sr-latn", "sr-Latn"),
        ("es-419", "es-419"),
        ("de-CH-1996", "de-CH-1996"),
        ("en-a-bbb-x-yz", "en-a-bbb-x-yz"),
        ("en-x-ab", "en-x-ab"),
        ("eng", "en"),
        ("fre", "fr"),
        ("fra-FR", "fr-FR"),
        ("ger", "de"),
        ("slv", "sl"),
        ("grc", "grc"),
        ("und", "und"),
    ],
)
def test_normalize_tag(tag: str, expected: str) -> None:
    assert normalize_tag(tag) == expected


@pytest.mark.parametrize(
    "tag",
    ["", "e", "*", "en_US", "en-", "-en", "en--gb", "english language", "1en", "ab-123456789"],
)
def test_normalize_tag_rejects_malformed(tag: str) -> None:
    assert normalize_tag(tag) is None


def test_primary_language() -> None:
    assert primary_language("en") == "en"
    assert primary_language("en-GB") == "en"
    assert primary_language("ZH-Hant") == "zh"


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("http://vocab.getty.edu/aat/300404670", "300404670"),
        ("https://vocab.getty.edu/aat/300404670", "300404670"),
        ("aat:300404670", "300404670"),
        ("http://vocab.getty.edu/aat/", None),
        ("http://vocab.getty.edu/aat/300404670/", None),
        ("http://vocab.getty.edu/page/aat/300404670", None),
        ("http://vocab.getty.edu/ulan/500011051", None),
        ("http://vocab.getty.edu/aat/３００", None),
    ],
)
def test_aat_id(uri: str, expected: str | None) -> None:
    assert aat_id(uri) == expected


def test_aat_language_tag() -> None:
    assert aat_language_tag(ENGLISH) == "en"
    assert aat_language_tag("https://vocab.getty.edu/aat/300388306") == "fr"
    assert aat_language_tag("aat:300389311") == "es"
    assert aat_language_tag("http://vocab.getty.edu/aat/300404670") is None
    assert aat_language_tag("https://www.wikidata.org/entity/Q1860") is None


def test_language_tag_prefers_the_notation() -> None:
    assert language_tag({"id": ENGLISH, "type": "Language", "notation": "en-GB"}) == "en-GB"
    assert language_tag({"id": "https://example.org/lang/1", "notation": "nl"}) == "nl"
    assert language_tag({"id": ENGLISH, "notation": "eng"}) == "en"


def test_language_tag_falls_back_to_the_aat_id() -> None:
    assert language_tag({"id": SPANISH, "type": "Language", "_label": "Spanish"}) == "es"
    assert language_tag({"id": ENGLISH, "notation": "not a tag"}) == "en"
    assert language_tag({"id": ENGLISH, "notation": ["en"]}) == "en"


@pytest.mark.parametrize(
    "language",
    [
        {"id": "https://linked-art.library.yale.edu/node/12d1608d", "_label": "English"},
        {"id": "http://vocab.getty.edu/aat/300387765", "type": "Language"},
        {"type": "Language", "_label": "English"},
        {"id": ENGLISH.replace("300388277", "300404670")},
        "en",
        None,
    ],
)
def test_unknown_language_is_und(language: Any) -> None:
    assert language_tag(language) == UNDETERMINED
