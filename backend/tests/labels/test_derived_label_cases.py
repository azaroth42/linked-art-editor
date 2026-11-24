"""The server's derived ``_label`` for the cases the browser's mirror is compared with (Step 1.19a).

The editor derives a draft's root ``_label`` in the browser (``frontend/src/crdt/derivedLabel.ts``)
exactly as ``lae.labels.derive`` does on the server. ``data/derived_label_cases.json`` holds:

- ``cases``: hand-made documents, each with the label language it is derived under, the
  ``label`` (``derive_label``), the ``derived`` document (``with_derived_label``) and the
  ``schema`` verdict of ``lae.validation.schema.validate_schema`` on it, in the form of
  ``tests/validation/data/schema_browser_cases.json``;
- ``corpus``: ``derive_label`` of every corpus record under ``en``, ``fr`` and ``de``;
- ``languages``: the two tables the mirror needs, ``AAT_LANGUAGE_TAGS`` and ``ISO_639_2_TO_1``.

``derivedLabel.test.ts`` must find exactly the same. The file is generated from the code below;
this test fails when it is out of date. Regenerate with
``uv run python -m tests.labels.test_derived_label_cases`` (from ``backend/``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Final

from lae.corpus import CORPUS_DIR, iter_corpus
from lae.labels.derive import derive_label, with_derived_label
from lae.labels.langtags import AAT_LANGUAGE_TAGS, ISO_639_2_TO_1
from lae.projection import assign_node_iris
from tests.validation.test_schema_browser_cases import _verdict

FIXTURE: Final = Path(__file__).parent / "data" / "derived_label_cases.json"
CORPUS_LANGUAGES: Final = ("en", "fr", "de")
RECORD: Final = "https://example.org/object/1"
NODE: Final = "/node/01937a4e-0000-7000-8000-0000000000{:02x}"
AAT: Final = "http://vocab.getty.edu/aat/"
SPELLINGS: Final = (AAT, "https://vocab.getty.edu/aat/", "aat:")
PRIMARY: Final = {"id": AAT + "300404670", "type": "Type", "_label": "Primary Name"}
ENGLISH: Final = "300388277"
FRENCH: Final = "300388306"
GERMAN: Final = "300388344"

WHITESPACE: Final = "".join(
    [chr(c) for c in range(0x09, 0x0E)]
    + [chr(c) for c in range(0x1C, 0x20)]
    + [" ", "\u0085", " ", " "]
    + [chr(c) for c in range(0x2000, 0x200B)]
    + [" ", " ", " ", " ", "　"]
)
"""The 29 code points of Python's whitespace."""

Document = dict[str, Any]


def lang(concept: str | None = None, notation: Any = None) -> Document:
    entry: Document = {"type": "Language"}
    if concept is not None:
        entry["id"] = concept
    if notation is not None:
        entry["notation"] = notation
    return entry


def name(content: Any, *, primary: bool = False, language: Any = None, **more: Any) -> Document:
    node: Document = {"type": "Name", "content": content}
    if primary:
        node["classified_as"] = [PRIMARY]
    if language is not None:
        node["language"] = language if isinstance(language, list) else [language]
    return {**node, **more}


def doc(*names: Any, label: str | None = None, **extra: Any) -> Document:
    """A ``HumanMadeObject`` with ``names`` as its ``identified_by`` and internal node IRIs."""
    out: Document = {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": RECORD,
        "type": "HumanMadeObject",
    }
    if label is not None:
        out["_label"] = label
    if names:
        out["identified_by"] = list(names)
    out.update(extra)
    counter = iter(range(256))
    return assign_node_iris(out, RECORD, mint=lambda: RECORD + NODE.format(next(counter)))


EN = lang(AAT + ENGLISH)
FR = lang(AAT + FRENCH)
DE = lang(AAT + GERMAN)

THREE_PRIMARIES: Final = (
    name("English title", primary=True, language=EN),
    name("Titre français", primary=True, language=FR),
    name("Deutscher Titel", primary=True, language=DE),
)


def _cases() -> list[tuple[str, str, Document]]:
    """``(name, language, document)``: every branch of test_derive.py's tests 1-4 under ``en``
    and of its tests 5a under ``fr`` or ``de``."""
    cases: list[tuple[str, str, Document]] = []

    def add(case: str, language: str, document: Document) -> None:
        cases.append((case, language, document))

    # 1. Selection.
    add(
        "the English primary name wins over an earlier French one",
        "en",
        doc(
            name("Alt", language=EN),
            name("Nom", primary=True, language=FR),
            name("Name", primary=True, language=EN),
            label="Stale label",
        ),
    )
    add(
        "any primary name beats a plain name",
        "en",
        doc(name("Plain", language=EN), name("Nom", primary=True, language=FR)),
    )
    for spelling in SPELLINGS:
        primary = {"id": spelling + "300404670", "type": "Type"}
        add(
            f"Primary Name spelled {spelling}",
            "en",
            doc(name("Plain"), name("Spelled", classified_as=[primary])),
        )
    add(
        "a single classification object counts",
        "en",
        doc(name("Plain"), name("Single", classified_as={"id": "aat:300404670", "type": "Type"})),
    )
    add(
        "another classification is not Primary Name",
        "en",
        doc(name("First"), name("Other", classified_as=[{"id": AAT + "300264273"}])),
    )
    add(
        "the first name when none is primary",
        "en",
        doc(name("Erste", language=DE), name("First", language=EN)),
    )
    add(
        "an identifier before the name does not count",
        "en",
        doc({"type": "Identifier", "content": "1937.1"}, name("Vase")),
    )
    add("no identified_by, a _label of its own", "en", doc(label="Bulk-loaded label"))
    add("no identified_by and no _label", "en", doc())
    add(
        "only identifiers",
        "en",
        doc({"type": "Identifier", "content": "1937.1"}, label="Kept"),
    )
    add("a blank content", "en", doc(name(""), label="Kept"))
    add("a whitespace-only content", "en", doc(name(WHITESPACE)))
    add("a content that is a number", "en", doc(name(42), label="Kept"))
    add("a content that is null", "en", doc(name(None), label="Kept"))
    add("a Name without content", "en", doc({"type": "Name"}, label="Kept"))
    add(
        "a lower-case type and a missing type",
        "en",
        doc({"type": "name", "content": "x"}, {"content": "y"}, name("Third")),
    )
    add(
        "malformed items before a usable name",
        "en",
        doc("a string", None, 7, ["nested"], True, name("Usable")),
    )
    add(
        "identified_by is a single Name object",
        "en",
        doc(label="Old", identified_by=name(" Single ", primary=True)),
    )
    add("identified_by is a string", "en", doc(label="Kept", identified_by="Not a name"))
    add(
        "a name of an embedded node does not count",
        "en",
        doc(
            label="Kept",
            produced_by={
                "type": "Production",
                "timespan": {"type": "TimeSpan", "identified_by": [name("1642")]},
            },
        ),
    )

    # 2. The language of a name (under en).
    def english_or_not(case: str, language: Any) -> None:
        add(
            case,
            "en",
            doc(
                name("French primary", primary=True, language=FR),
                name("Tested", primary=True, language=language),
            ),
        )

    english_or_not("notation en", lang(notation="en"))
    english_or_not("notation en-GB", lang(notation="en-GB"))
    english_or_not("notation eng", lang(notation="eng"))
    for spelling in SPELLINGS:
        english_or_not(f"English id spelled {spelling}", lang(spelling + ENGLISH))
    english_or_not("a malformed notation falls back to the id", lang(AAT + ENGLISH, "not a tag"))
    english_or_not("a notation that is not a string", lang(AAT + ENGLISH, ["en"]))
    english_or_not("a French notation with the English id", lang(AAT + ENGLISH, "fr"))
    english_or_not("only the first language entry is read", [FR, EN])
    english_or_not("a first language entry that is not an object", ["en", EN])
    english_or_not("an id with a non-digit local part", lang(AAT + ENGLISH + "x"))
    english_or_not("an id with non-ASCII digits", lang(AAT + "３００３８８２７７"))
    english_or_not("a non-AAT language id", lang("https://example.org/english"))
    english_or_not("an AAT id outside the table", lang(AAT + "300404670"))
    english_or_not("a language entry without id or notation", lang())

    # 3. Normalization.
    add(
        "every whitespace code point, collapsed",
        "en",
        doc(name(WHITESPACE + "A" + WHITESPACE.join("BC") + WHITESPACE)),
    )
    add("U+FEFF and U+200B are kept", "en", doc(name("﻿A ​ B​")))
    add(
        "no Unicode normalization or case folding",
        "en",
        doc(name("Café ÉTÉ \U0001f3a8")),
    )
    add("no truncation", "en", doc(name("x" * 10_000)))

    # 4. The root _label.
    add("a stale _label is replaced", "en", doc(name("New title"), label="Old title"))
    add("an equal _label is kept as it is", "en", doc(name("Same"), label="Same"))
    add("a _label that is not a string", "en", doc(name("Title"), _label=["not", "a string"]))

    # 5a. The label language.
    for language in ("en", "fr", "de", "it"):
        add(f"three primary names under {language}", language, doc(*THREE_PRIMARIES))
    for spelling in SPELLINGS:
        aat_only = doc(
            name("English", primary=True, language=lang(spelling + ENGLISH)),
            name("Français", primary=True, language=lang(spelling + FRENCH)),
            name("Deutsch", primary=True, language=lang(spelling + GERMAN)),
        )
        add(f"only AAT ids spelled {spelling}, French", "fr", aat_only)
        add(f"only AAT ids spelled {spelling}, German", "de", aat_only)
    for notation in ("fr", "FR", "fr-CA", "fra", "fre", " fr "):
        add(
            f"French notation {notation!r}",
            "fr",
            doc(
                name("English", primary=True, language=EN),
                name("Tested", primary=True, language=lang(AAT + GERMAN, notation)),
            ),
        )
    # The notation is stripped over Python's whitespace, not JavaScript's: U+001C and U+0085
    # are stripped (JavaScript's trim() keeps them); U+FEFF is not (trim() removes it), so that
    # notation is malformed and the id decides.
    for case, notation, expected in (
        ("a notation padded with U+001C and U+0085 is stripped", "\u001cfr\u0085", "Tested"),
        ("a notation padded with U+FEFF is malformed: the id decides", "\ufefffr", "English"),
    ):
        padded = doc(
            name("English", primary=True, language=EN),
            name("Tested", primary=True, language=lang(AAT + GERMAN, notation)),
        )
        assert derive_label(padded, "fr") == expected, case
        add(case, "fr", padded)
    for notation in ("en", "english"):
        add(
            f"notation {notation!r} with the French id is not French",
            "fr",
            doc(
                name("English", primary=True, language=EN),
                name("Tested", primary=True, language=lang(AAT + FRENCH, notation)),
            ),
        )
    add(
        "German notations deu and ger",
        "de",
        doc(
            name("English", primary=True, language=EN),
            name("Deu", primary=True, language=lang(notation="deu")),
            name("Ger", primary=True, language=lang(notation="ger")),
        ),
    )
    add(
        "no name in the label language: the first primary name",
        "fr",
        doc(name("Plain", language=EN), name("English", primary=True, language=EN)),
    )
    add(
        "no name in the label language and no primary name: the first name",
        "de",
        doc(name("First", language=EN), name("Second", language=FR)),
    )
    add(
        "a plain name in the label language does not beat a primary name",
        "fr",
        doc(name("Plain", language=FR), name("English", primary=True, language=EN)),
    )
    add(
        "two primary names in the label language: the first",
        "fr",
        doc(
            name("Premier", primary=True, language=FR),
            name("Second", primary=True, language=FR),
        ),
    )
    add("no usable name under fr keeps the _label", "fr", doc(name(" "), label="Kept"))
    for language in ("EN", "en-GB", ""):
        add(
            f"the unvalidated language {language!r} matches nothing",
            language,
            doc(
                name("Plain", language=lang(notation="EN")),
                name("First primary", primary=True, language=FR),
                name("English", primary=True, language=lang(notation="en-GB")),
            ),
        )
    return cases


FIXTURE_CASES: Final = [
    {"name": case, "language": language, "document": document}
    for case, language, document in _cases()
]
"""The hand-made cases, also used by ``test_derive.py``."""


def build() -> dict[str, Any]:
    """The contents of ``FIXTURE``."""
    cases = []
    for case in FIXTURE_CASES:
        document = copy.deepcopy(case["document"])
        derived = with_derived_label(document, case["language"])
        cases.append(
            {
                "name": case["name"],
                "language": case["language"],
                "document": document,
                "label": derive_label(document, case["language"]),
                "derived": derived,
                "schema": _verdict(derived),
            }
        )
    corpus: dict[str, dict[str, str | None]] = {language: {} for language in CORPUS_LANGUAGES}
    for path, record in iter_corpus():
        key = path.relative_to(CORPUS_DIR).as_posix()
        for language in CORPUS_LANGUAGES:
            corpus[language][key] = derive_label(record, language)
    return {
        "description": __doc__.split("\n\n")[0] if __doc__ else "",
        "cases": cases,
        "corpus": corpus,
        "languages": {"aat": dict(AAT_LANGUAGE_TAGS), "iso639_2": dict(ISO_639_2_TO_1)},
    }


def _text(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def test_the_fixture_is_up_to_date() -> None:
    assert FIXTURE.read_text(encoding="utf-8") == _text(build()), (
        "regenerate: uv run python -m tests.labels.test_derived_label_cases"
    )


def test_the_cases_cover_what_the_browser_must_mirror() -> None:
    data = build()
    cases = {case["name"]: case for case in data["cases"]}
    assert len(cases) == len(data["cases"])
    # One document under en, fr and de with three different labels; another language: the first.
    labels = [cases[f"three primary names under {lang}"]["label"] for lang in ("en", "fr", "de")]
    assert labels == ["English title", "Titre français", "Deutscher Titel"]
    assert cases["three primary names under it"]["label"] == "English title"
    # Without a usable name: null, the document unchanged, and the schema's verdict on _label.
    missing = cases["no identified_by and no _label"]
    assert missing["label"] is None and missing["derived"] == missing["document"]
    assert {"code": "LAE-SCHEMA-required", "path": "", "missing": ["_label"]} in missing["schema"]
    kept = cases["no identified_by, a _label of its own"]
    assert kept["derived"]["_label"] == "Bulk-loaded label"
    # A name gives a label and no missing-_label finding.
    for case in data["cases"]:
        if case["label"] is not None:
            assert case["derived"]["_label"] == case["label"], case["name"]
            assert not any(
                found["code"] == "LAE-SCHEMA-required" and "_label" in found.get("missing", [])
                for found in case["schema"]
            ), case["name"]
    assert {case["language"] for case in data["cases"]} >= {"en", "fr", "de"}
    assert len(cases["no truncation"]["label"]) == 10_000
    for language in CORPUS_LANGUAGES:
        assert len(data["corpus"][language]) > 200
    # Some corpus records are labelled differently under another language.
    assert data["corpus"]["en"] != data["corpus"]["fr"]
    assert len(data["languages"]["aat"]) == 57


if __name__ == "__main__":
    FIXTURE.parent.mkdir(exist_ok=True)
    FIXTURE.write_text(_text(build()), encoding="utf-8")
