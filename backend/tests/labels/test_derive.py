"""Tests for ``lae.labels.derive`` and the name selection it shares with ``labels_from_record``
(Step 1.19a; contract ``docs/contracts/1.19a-derived-label.md`` §5, tests 1-5a).

Tests that are not about the label language pass ``"en"`` explicitly.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any, Final

import pytest

from lae.corpus import iter_corpus
from lae.labels.derive import (
    DEFAULT_LABEL_LANGUAGE,
    LABEL_KEY,
    derive_label,
    normalize_label,
    with_derived_label,
)
from lae.labels.extract import NameLabel, iter_names, labels_from_record
from lae.labels.langtags import primary_language
from tests.labels.test_derived_label_cases import FIXTURE_CASES

AAT: Final = "http://vocab.getty.edu/aat/"
AAT_SPELLINGS: Final = (AAT, "https://vocab.getty.edu/aat/", "aat:")
PRIMARY_ID: Final = "300404670"
ENGLISH_ID: Final = "300388277"
FRENCH_ID: Final = "300388306"
GERMAN_ID: Final = "300388344"

WHITESPACE: Final = (
    [chr(c) for c in range(0x09, 0x0E)]
    + [chr(c) for c in range(0x1C, 0x20)]
    + [" ", "\u0085", " ", " "]
    + [chr(c) for c in range(0x2000, 0x200B)]
    + [" ", " ", " ", " ", "　"]
)
"""The 29 code points of ``str.isspace``, as the contract lists them."""

LANGUAGES: Final = ("en", "fr", "de")


def lang(concept: str | None = None, notation: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": "Language"}
    if concept is not None:
        entry["id"] = concept
    if notation is not None:
        entry["notation"] = notation
    return entry


def name(
    content: Any,
    *,
    primary: bool = False,
    language: dict[str, Any] | list[Any] | None = None,
    primary_id: str = AAT + PRIMARY_ID,
) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "Name", "content": content}
    if primary:
        node["classified_as"] = [{"id": primary_id, "type": "Type"}]
    if language is not None:
        node["language"] = language if isinstance(language, list) else [language]
    return node


def record(*names: Any, **extra: Any) -> dict[str, Any]:
    return {"id": "https://example.org/id/1", "type": "HumanMadeObject", **extra,
            "identified_by": list(names)}  # fmt: skip


EN = lang(AAT + ENGLISH_ID)
FR = lang(AAT + FRENCH_ID)
DE = lang(AAT + GERMAN_ID)


def corpus_records() -> list[tuple[str, dict[str, Any]]]:
    return [(path.name, data) for path, data in iter_corpus()]


# --- 1. selection ------------------------------------------------------------------------------


def test_the_english_primary_name_wins() -> None:
    doc = record(
        name("Alt", language=EN),
        name("Nom", primary=True, language=FR),
        name("Name", primary=True, language=EN),
        name("Second English", primary=True, language=EN),
    )
    assert derive_label(doc, "en") == "Name"


def test_any_primary_name_beats_a_plain_name() -> None:
    doc = record(name("Plain", language=EN), name("Nom", primary=True, language=FR))
    assert derive_label(doc, "en") == "Nom"
    # A primary name without any language is still a primary name.
    assert derive_label(record(name("Plain"), name("Bare", primary=True)), "en") == "Bare"
    # Every AAT spelling of Primary Name counts.
    for spelling in AAT_SPELLINGS:
        doc = record(name("Plain"), name("Spelled", primary=True, primary_id=spelling + PRIMARY_ID))
        assert derive_label(doc, "en") == "Spelled", spelling
    # Another classification is not Primary Name.
    other = record(name("First"), name("Other", primary=True, primary_id=AAT + "300264273"))
    assert derive_label(other, "en") == "First"


def test_the_first_name_when_none_is_primary() -> None:
    doc = record(name("Erste", language=DE), name("First", language=EN), name("Second"))
    assert derive_label(doc, "en") == "Erste"
    # Identifiers never count, even before the name.
    doc = record({"type": "Identifier", "content": "1937.1"}, name("Vase"))
    assert derive_label(doc, "en") == "Vase"


@pytest.mark.parametrize(
    "doc",
    [
        {"id": "x", "type": "HumanMadeObject"},
        record(),
        record({"type": "Identifier", "content": "1937.1"}),
        record(name("")),
        record(name(" \t　\n")),
        record(name(42)),
        record(name(None)),
        record(name(["a list"])),
        record({"type": "Name"}),
        record({"type": "name", "content": "lower-case type"}),
        record({"content": "no type"}),
        record("a string", None, 7, ["nested"], True),
        {"id": "x", "identified_by": None},
        {"id": "x", "identified_by": "a string"},
        {"id": "x", "identified_by": {"type": "Identifier", "content": "single"}},
        # Names of embedded nodes never count.
        {"id": "x", "produced_by": {"type": "Production", "identified_by": [name("Embedded")]}},
    ],
    ids=lambda doc: json.dumps(doc)[:60],
)
def test_no_usable_name_gives_none(doc: dict[str, Any]) -> None:
    assert derive_label(doc, "en") is None
    assert with_derived_label(doc, "en") is doc


def test_a_single_name_object_counts_as_a_one_item_array() -> None:
    doc = {"id": "x", "identified_by": name("  Single   object  ", primary=True)}
    assert derive_label(doc, "en") == "Single object"
    # The same for one classification and one language.
    single = {
        "type": "Name",
        "content": "Seul",
        "classified_as": {"id": AAT + PRIMARY_ID},
        "language": FR,
    }
    assert derive_label(record(name("Plain"), single), "fr") == "Seul"


# --- 2. the language of a name -----------------------------------------------------------------


def test_language_comes_from_the_first_language_entry() -> None:
    def english(language: dict[str, Any] | list[Any]) -> bool:
        doc = record(
            name("Other", primary=True, language=FR),
            name("Tested", primary=True, language=language),
        )
        return derive_label(doc, "en") == "Tested"

    assert english(lang(notation="en"))
    assert english(lang(notation="en-GB"))
    assert english(lang(notation="eng"))
    for spelling in AAT_SPELLINGS:
        assert english(lang(spelling + ENGLISH_ID)), spelling
    # A malformed notation falls back to the id.
    assert english(lang(AAT + ENGLISH_ID, notation="not a tag"))
    assert english(lang(AAT + ENGLISH_ID, notation=["en"]))
    # A well-formed French notation decides alone, whatever the id says.
    assert not english(lang(AAT + ENGLISH_ID, notation="fr"))
    # Only the first entry is read.
    assert english([EN, FR])
    assert not english([FR, EN])
    assert not english(["not an object", EN])
    # An id that is not an AAT language of the table, or not AAT at all, is no language.
    assert not english(lang(AAT + "300388277x"))
    assert not english(lang("https://example.org/english"))
    assert not english(lang(AAT + PRIMARY_ID))


# --- 3. normalization ------------------------------------------------------------------------


def test_whitespace_is_normalized_over_exactly_the_29_code_points() -> None:
    assert len(WHITESPACE) == 29
    assert all(c.isspace() for c in WHITESPACE)
    assert sum(chr(c).isspace() for c in range(0x110000)) == 29
    for char in WHITESPACE:
        text = f"{char}{char}A{char}{char}{char}B{char}"
        assert normalize_label(text) == "A B", hex(ord(char))
        assert derive_label(record(name(text)), "en") == "A B", hex(ord(char))
        # A content of only this whitespace is no usable name.
        assert derive_label(record(name(char * 3)), "en") is None, hex(ord(char))
    for kept in ("﻿", "​"):
        assert normalize_label(f"{kept}A{kept}B{kept}") == f"{kept}A{kept}B{kept}"
        assert derive_label(record(name(kept)), "en") == kept
    # Nothing else changes: no Unicode normalization, no case folding.
    assert normalize_label("Café  ÉTÉ") == "Café ÉTÉ"
    assert normalize_label("  a \t\n b  ") == "a b"


def test_no_truncation() -> None:
    long = "x" * 10_000
    assert derive_label(record(name(long)), "en") == long
    assert with_derived_label(record(name(long)), "en")[LABEL_KEY] == long


# --- 4. the root _label ------------------------------------------------------------------------


def test_the_root_label_is_never_an_input() -> None:
    names = (name("Nom", primary=True, language=FR), name("Name", primary=True, language=EN))
    for label in (None, "", "Stale", "Name", 42, ["x"]):
        doc = record(*names) if label is None else record(*names, _label=label)
        assert derive_label(doc, "en") == "Name"
        assert with_derived_label(doc, "en")[LABEL_KEY] == "Name"
    assert derive_label(record(_label="Only a label"), "en") is None


def test_with_derived_label_is_idempotent_and_does_not_modify_its_argument() -> None:
    documents = [record(name(" Spaced\tname "), _label="Old"), record(name("New"))]
    documents += [case["document"] for case in FIXTURE_CASES]
    for doc in documents:
        for language in LANGUAGES:
            before = copy.deepcopy(doc)
            once = with_derived_label(doc, language)
            assert doc == before
            twice = with_derived_label(once, language)
            assert twice == once
            assert twice is once
            if once is not doc:
                assert {k: v for k, v in once.items() if k != LABEL_KEY} == {
                    k: v for k, v in doc.items() if k != LABEL_KEY
                }
    # The same object when the label is already right.
    right = record(name("Right"), _label="Right")
    assert with_derived_label(right, "en") is right
    changed = with_derived_label(record(name(" Spaced\tname "), _label="Old"), "en")
    assert changed[LABEL_KEY] == "Spaced name"


def test_without_a_usable_name_the_document_is_left_alone() -> None:
    kept = record(name(""), _label="Kept")
    assert with_derived_label(kept, "en") is kept
    assert with_derived_label(kept, "en")[LABEL_KEY] == "Kept"
    bare = record(name("  "))
    assert with_derived_label(bare, "en") is bare
    assert LABEL_KEY not in with_derived_label(bare, "en")
    for doc in ({"id": "x"}, record(), record(name(None), _label="")):
        result = with_derived_label(doc, "en")
        assert result is doc
        assert result.get(LABEL_KEY) in (None, "")  # never written as "" or null
        assert LABEL_KEY not in doc or doc[LABEL_KEY] == ""


# --- 5. agreement with entity_labels -----------------------------------------------------------


def test_derived_label_is_the_normalized_default_label_for_every_corpus_record() -> None:
    named = 0
    for path, data in corpus_records():
        first = None
        for language in LANGUAGES:
            labels, default = labels_from_record(data, language)
            label = derive_label(data, language)
            if label is not None:
                assert label == normalize_label(default), (path, language)
            if first is None:
                first = labels
            assert labels == first, (path, language)
        named += derive_label(data, "en") is not None
    assert named >= 100


def _old_default_label(jsonld: Mapping[str, Any], names: list[NameLabel]) -> str:
    """``lae.labels.extract._default_label`` as it was before Step 1.19a (English hard-coded)."""
    primaries = [name for name in names if name.primary]
    for name in primaries:
        if primary_language(name.language) == "en":
            return name.content
    if primaries:
        return primaries[0].content
    if names:
        return names[0].content
    label = jsonld.get("_label")
    if isinstance(label, str) and label.strip():
        return label
    record_id = jsonld.get("id")
    return record_id if isinstance(record_id, str) else ""


def _old_labels_from_record(jsonld: Mapping[str, Any]) -> tuple[dict[str, str], str]:
    """``labels_from_record`` as it was before Step 1.19a."""
    names = list(iter_names(jsonld))
    labels: dict[str, str] = {}
    primary_languages: set[str] = set()
    for name in names:
        if name.primary and name.language not in primary_languages:
            labels[name.language] = name.content
            primary_languages.add(name.language)
        elif name.language not in labels:
            labels[name.language] = name.content
    return labels, _old_default_label(jsonld, names)


def test_labels_from_record_is_unchanged_for_the_corpus() -> None:
    records = corpus_records()
    assert len(records) > 200
    for path, data in records:
        assert labels_from_record(data, "en") == _old_labels_from_record(data), path
    for case in FIXTURE_CASES:
        document = case["document"]
        assert labels_from_record(document, "en") == _old_labels_from_record(document)


# --- 5a. the label language --------------------------------------------------------------------


THREE_PRIMARIES: Final = record(
    name("English", primary=True, language=EN),
    name("Français", primary=True, language=FR),
    name("Deutsch", primary=True, language=DE),
)


def test_the_primary_name_in_the_label_language_wins() -> None:
    assert derive_label(THREE_PRIMARIES, "en") == "English"
    assert derive_label(THREE_PRIMARIES, "fr") == "Français"
    assert derive_label(THREE_PRIMARIES, "de") == "Deutsch"
    assert derive_label(THREE_PRIMARIES, "it") == "English"
    for language, label in (("en", "English"), ("fr", "Français"), ("de", "Deutsch")):
        assert labels_from_record(THREE_PRIMARIES, language)[1] == label
    # A plain name in the label language does not beat a primary name in another one.
    doc = record(name("Plain", language=FR), name("Primary", primary=True, language=EN))
    assert derive_label(doc, "fr") == "Primary"
    # No name in the label language: the first primary name, else the first name.
    assert derive_label(record(name("A", language=EN), name("B", language=EN)), "fr") == "A"


def test_a_name_with_only_an_aat_language_id_matches_the_setting() -> None:
    for spelling in AAT_SPELLINGS:
        doc = record(
            name("English", primary=True, language=lang(spelling + ENGLISH_ID)),
            name("Français", primary=True, language=lang(spelling + FRENCH_ID)),
            name("Deutsch", primary=True, language=lang(spelling + GERMAN_ID)),
        )
        assert derive_label(doc, "fr") == "Français", spelling
        assert derive_label(doc, "de") == "Deutsch", spelling
        assert derive_label(doc, "en") == "English", spelling


def test_notation_forms_of_the_label_language() -> None:
    def french(language: dict[str, Any]) -> bool:
        doc = record(
            name("English", primary=True, language=EN),
            name("Tested", primary=True, language=language),
        )
        return derive_label(doc, "fr") == "Tested"

    for notation in ("fr", "FR", "fr-CA", "fra", "fre", " fre ", " fr "):
        assert french(lang(notation=notation)), notation
        assert french(lang(AAT + GERMAN_ID, notation=notation)), notation
    # A well-formed notation decides alone, and the id is not consulted.
    assert not french(lang(AAT + FRENCH_ID, notation="en"))
    assert not french(lang(AAT + FRENCH_ID, notation="english"))
    # A malformed notation falls back to the id.
    assert french(lang(AAT + FRENCH_ID, notation="fr_FR"))


def test_the_language_never_decides_whether_there_is_a_label() -> None:
    documents = [data for _, data in corpus_records()]
    documents += [case["document"] for case in FIXTURE_CASES]
    for doc in documents:
        verdicts = {derive_label(doc, language) is None for language in (*LANGUAGES, "it", "zz")}
        assert len(verdicts) == 1


def test_an_unvalidated_language_matches_nothing_and_does_not_raise() -> None:
    doc = record(
        name("Plain", language=lang(notation="EN")),
        name("First primary", primary=True, language=FR),
        name("English", primary=True, language=lang(notation="en-GB")),
    )
    for language in ("EN", "en-GB", ""):
        assert derive_label(doc, language) == "First primary", language
        assert labels_from_record(doc, language)[1] == "First primary", language
    assert derive_label(doc, "en") == "English"


def test_the_labels_by_language_do_not_depend_on_the_setting() -> None:
    documents = [THREE_PRIMARIES] + [data for _, data in corpus_records()]
    for doc in documents:
        first = labels_from_record(doc, "en")[0]
        for language in ("fr", "de", "it", "EN", ""):
            assert labels_from_record(doc, language)[0] == first


def test_the_default_label_language_is_english() -> None:
    assert DEFAULT_LABEL_LANGUAGE == "en"
