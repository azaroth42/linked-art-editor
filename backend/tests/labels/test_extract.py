"""Tests for ``lae.labels.extract.labels_from_record``."""

from __future__ import annotations

from typing import Any

import pytest

from lae.corpus import iter_corpus
from lae.labels.extract import is_primary_name, iter_names, labels_from_record
from tests.labels.conftest import (
    HACHA,
    MONA_LISA,
    NIGHT_WATCH,
    SPANISH_ONLY,
    YCBA_PERSON,
    YUAG_OBJECT,
    corpus,
)

PRIMARY = {"id": "http://vocab.getty.edu/aat/300404670", "type": "Type", "_label": "Primary Name"}
ALTERNATE = {"id": "http://vocab.getty.edu/aat/300264273", "type": "Type", "_label": "Alternate"}
LANG = {
    "en": {"id": "http://vocab.getty.edu/aat/300388277", "type": "Language"},
    "fr": {"id": "http://vocab.getty.edu/aat/300388306", "type": "Language"},
    "de": {"id": "http://vocab.getty.edu/aat/300388344", "type": "Language"},
    "ja": {"id": "http://vocab.getty.edu/aat/300388486", "type": "Language"},
}


def name(content: Any, language: str | None = None, *, primary: bool = False) -> dict[str, Any]:
    """A ``Name`` node with optional language and Primary Name classification."""
    node: dict[str, Any] = {"type": "Name", "content": content}
    if primary:
        node["classified_as"] = [PRIMARY]
    if language is not None:
        node["language"] = [LANG[language]]
    return node


def record(*names: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """A minimal record with the given names."""
    return {"id": "https://example.org/id/1", "type": "HumanMadeObject", **extra,
            "identified_by": list(names)}  # fmt: skip


# --- corpus records in several languages ----------------------------------------------------


def test_mona_lisa_has_english_french_and_italian_labels() -> None:
    labels, default = labels_from_record(corpus(MONA_LISA))
    assert labels == {"en": "Mona Lisa", "fr": "La Joconde", "it": "La Gioconda"}
    assert default == "Mona Lisa"


def test_night_watch_has_english_and_dutch_labels() -> None:
    labels, default = labels_from_record(corpus(NIGHT_WATCH))
    assert labels == {"en": "The Night Watch", "nl": "De Nachtwacht"}
    assert default == "The Night Watch"


def test_first_language_of_a_name_is_its_language() -> None:
    # The only name lists Spanish, then English: it is a Spanish label and, there being no
    # English primary name, also the default.
    labels, default = labels_from_record(corpus(HACHA))
    assert labels == {"es": "Hacha (Ceremonial Axe)"}
    assert default == "Hacha (Ceremonial Axe)"


def test_spanish_only_record() -> None:
    assert labels_from_record(corpus(SPANISH_ONLY)) == ({"es": "Hacha"}, "Hacha")


def test_language_from_the_aat_id_without_notation() -> None:
    labels, default = labels_from_record(corpus(YUAG_OBJECT))
    assert labels == {"en": "Female Sensibility"}
    assert default == "Female Sensibility"


def test_names_without_language_are_und() -> None:
    labels, default = labels_from_record(corpus(YCBA_PERSON))
    assert labels == {"en": "Haveill Gillbank", "und": "Gillbank Haveill, 18th–19th centuries"}
    assert default == "Haveill Gillbank"


def test_record_without_names_uses_its_label() -> None:
    data = corpus("institutional/yuag-agt-36724.json")
    assert labels_from_record(data) == ({}, data["_label"])


def test_record_without_names_or_label_uses_its_id() -> None:
    data = corpus("linkedart/event-1.json")
    assert "_label" not in data
    assert labels_from_record(data) == ({}, data["id"])


def test_every_corpus_record_has_a_default_label() -> None:
    by_language: dict[str, int] = {}
    for path, data in iter_corpus():
        labels, default = labels_from_record(data)
        assert isinstance(default, str) and default.strip(), path
        contents = {n.content for n in iter_names(data)}
        assert set(labels.values()) <= contents, path
        if contents:
            assert default in contents, path
        for tag in labels:
            by_language[tag] = by_language.get(tag, 0) + 1
    assert {"en", "fr", "it", "nl", "es", "und"} <= by_language.keys()


def test_english_primary_name_is_the_default_wherever_there_is_one() -> None:
    seen = 0
    for path, data in iter_corpus():
        english_primaries = [n for n in iter_names(data) if n.primary and n.language == "en"]
        if english_primaries:
            assert labels_from_record(data)[1] == english_primaries[0].content, path
            seen += 1
    assert seen >= 15


# --- precedence rules ------------------------------------------------------------------------


def test_primary_name_wins_within_a_language() -> None:
    labels, _ = labels_from_record(
        record(name("Alt", "en"), name("Main", "en", primary=True), name("Later", "en"))
    )
    assert labels == {"en": "Main"}


def test_first_name_of_a_language_without_primary() -> None:
    labels, _ = labels_from_record(record(name("First", "fr"), name("Second", "fr")))
    assert labels == {"fr": "First"}


def test_first_primary_name_of_a_language_wins() -> None:
    labels, _ = labels_from_record(
        record(name("One", "de", primary=True), name("Two", "de", primary=True))
    )
    assert labels == {"de": "One"}


def test_default_is_the_english_primary_name_even_if_not_first() -> None:
    _, default = labels_from_record(
        record(name("Nom", "fr", primary=True), name("Alt", "en"), name("Name", "en", primary=True))
    )
    assert default == "Name"


def test_english_variant_counts_as_english() -> None:
    gb = name("Colour", primary=True) | {"language": [LANG["en"] | {"notation": "en-GB"}]}
    labels, default = labels_from_record(record(name("Farbe", "de", primary=True), gb))
    assert labels == {"de": "Farbe", "en-GB": "Colour"}
    assert default == "Colour"


def test_default_is_any_primary_name_without_an_english_one() -> None:
    _, default = labels_from_record(
        record(name("Alt", "en"), name("名前", "ja", primary=True), name("Nom", "fr", primary=True))
    )
    assert default == "名前"


def test_default_is_the_first_name_without_any_primary() -> None:
    _, default = labels_from_record(record(name("Erste", "de"), name("First", "en")))
    assert default == "Erste"


def test_default_falls_back_to_label_then_id() -> None:
    assert labels_from_record(record(_label="Root label")) == ({}, "Root label")
    assert labels_from_record(record(_label="  ")) == ({}, "https://example.org/id/1")
    assert labels_from_record({"type": "Person"}) == ({}, "")


def test_notation_beats_the_aat_language() -> None:
    swiss = name("Grüezi", primary=True) | {"language": [LANG["de"] | {"notation": "de-CH"}]}
    assert labels_from_record(record(swiss))[0] == {"de-CH": "Grüezi"}


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "Identifier", "content": "INV. 779"},
        {"type": "Name"},
        {"type": "Name", "content": ""},
        {"type": "Name", "content": "   "},
        {"type": "Name", "content": ["not", "a", "string"]},
        {"content": "untyped"},
        "a string",
    ],
)
def test_unusable_entries_are_ignored(entry: Any) -> None:
    labels, default = labels_from_record(record(entry, name("Kept", "en")))
    assert (labels, default) == ({"en": "Kept"}, "Kept")


def test_single_objects_are_treated_as_arrays() -> None:
    data = {
        "id": "https://example.org/id/2",
        "identified_by": {
            "type": "Name",
            "content": "Solo",
            "classified_as": PRIMARY,
            "language": LANG["fr"],
        },
    }
    assert labels_from_record(data) == ({"fr": "Solo"}, "Solo")


def test_is_primary_name_accepts_every_aat_spelling() -> None:
    for uri in ("http://vocab.getty.edu/aat/300404670", "https://vocab.getty.edu/aat/300404670",
                "aat:300404670"):  # fmt: skip
        assert is_primary_name({"classified_as": [ALTERNATE, {"id": uri}]})
    assert not is_primary_name({"classified_as": [ALTERNATE]})
    assert not is_primary_name({"classified_as": [{"_label": "Primary Name"}]})
    assert not is_primary_name({})


def test_input_is_not_modified() -> None:
    data = corpus(MONA_LISA)
    before = corpus(MONA_LISA)
    labels_from_record(data)
    assert data == before
