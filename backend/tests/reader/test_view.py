"""The Public Reader's view model and rendered page, without a database (``lae.reader``)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from lae.corpus import CORPUS_DIR, corpus_files, load_record
from lae.reader.i18n import catalogue_for
from lae.reader.render import render_page
from lae.reader.view import PageVersion, RecordPage, Section, Value, build_page

BASE = "https://data.test/id/"
RECORD_ID = UUID("01937a4e-0000-7000-8000-000000000001")
ARTIST_ID = UUID("01937a4e-0000-7000-8000-000000000002")
URI = f"{BASE}{RECORD_ID}"
ARTIST = f"{BASE}{ARTIST_ID}"
AAT = "http://vocab.getty.edu/aat/"
JSONLD_TYPE = 'application/ld+json;profile="https://linked.art/ns/v1/linked-art.json"'
PUBLISHED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def ref(uri: str, label: str | None = None, kind: str = "Type") -> dict[str, Any]:
    node: dict[str, Any] = {"id": uri, "type": kind}
    if label is not None:
        node["_label"] = label
    return node


def language(code: str) -> dict[str, Any]:
    ids = {"en": "300388277", "fr": "300388306", "el": "300389734"}
    return ref(f"{AAT}{ids[code]}", None, "Language")


KYLIX: dict[str, Any] = {
    "@context": "https://linked.art/ns/v1/linked-art.json",
    "id": URI,
    "type": "HumanMadeObject",
    "_label": "Kylix",
    "identified_by": [
        {
            "type": "Name",
            "classified_as": [ref(f"{AAT}300404670", "Primary Name")],
            "content": "Red-figure kylix",
            "language": [language("en")],
        },
        {"type": "Name", "content": "Coupe à figures rouges", "language": [language("fr")]},
        {
            "type": "Identifier",
            "classified_as": [ref(f"{AAT}300312355", "Accession Number")],
            "content": "1913.169",
            "language": [language("en")],
        },
    ],
    "classified_as": [ref(f"{AAT}300132254", "kylikes"), ref("urn:x-local:type", None)],
    "produced_by": {
        "type": "Production",
        "carried_out_by": [ref(ARTIST, "Douris", "Person")],
        "part": [
            {
                "type": "Production",
                "classified_as": [ref(f"{AAT}300025103", "potter")],
                "carried_out_by": [ref("https://example.org/python", "Python", "Person")],
            }
        ],
        "timespan": {
            "type": "TimeSpan",
            "begin_of_the_begin": "-0479-01-01T00:00:00Z",
            "end_of_the_end": "-0469-12-31T23:59:59Z",
        },
        "took_place_at": [ref("https://example.org/athens", "Athens", "Place")],
        "technique": [ref(f"{AAT}300069471", "red-figure")],
    },
    "made_of": [ref(f"{AAT}300010669", "terracotta", "Material")],
    "dimension": [
        {
            "type": "Dimension",
            "classified_as": [ref(f"{AAT}300055624", "diameter")],
            "value": 1234.5,
            "unit": ref(f"{AAT}300379098", "centimeters", "MeasurementUnit"),
        },
        {"type": "Dimension", "value": 7},
        {"type": "Dimension", "value": "tall"},
    ],
    "referred_to_by": [
        {
            "type": "LinguisticObject",
            "classified_as": [ref(f"{AAT}300435416", "Description")],
            "content": "Ἐπίκτητος ἔγραψεν",
            "language": [language("el")],
        },
        ref("https://example.org/catalogue", "Catalogue of Vases", "LinguisticObject"),
    ],
    "current_owner": [ref("https://example.org/museum", "The Museum", "Group")],
    "member_of": [ref(f"{BASE}01937a4e-0000-7000-8000-000000000003", None, "Set")],
    "x_unknown_property": [ref("https://example.org/thing", "Thing", "Type")],
}


def page_of(
    record: dict[str, Any],
    *,
    chain: tuple[str, ...] = ("en",),
    languages: dict[str, str] | None = None,
    version: PageVersion | None = None,
    signed_in: bool = False,
) -> RecordPage:
    return build_page(
        record,
        record_id=RECORD_ID,
        chain=list(chain),
        catalogue=catalogue_for(list(chain)),
        base_uri=BASE,
        label_languages=languages or {},
        version=version or PageVersion(None, 1, PUBLISHED),
        signed_in=signed_in,
        script_src="/reader/assets/reader.js",
        jsonld_type=JSONLD_TYPE,
    )


def section(page: RecordPage, slug: str) -> Section:
    return next(found for found in page.sections if found.slug == slug)


def terms(found: Section) -> dict[str, tuple[Value, ...]]:
    return {entry.term: entry.values for entry in found.entries}


def test_sections_in_order() -> None:
    page = page_of(KYLIX)
    assert [found.slug for found in page.sections] == [
        "names",
        "identifiers",
        "classifications",
        "produced_by",
        "materials",
        "dimensions",
        "statements",
        "related",
    ]
    assert [found.heading for found in page.sections][:3] == [
        "Names",
        "Identifiers",
        "Classifications",
    ]


def test_title_in_the_callers_language() -> None:
    assert page_of(KYLIX).title == Value("Red-figure kylix", "en")
    french = page_of(KYLIX, chain=("fr", "en"))
    assert french.title == Value("Coupe à figures rouges", "fr")
    assert french.page_title == "Coupe à figures rouges – Laertes"
    assert french.lang == "en"  # the reader has English strings only
    assert french.class_label == "Object"


def test_untitled_record() -> None:
    record = {"id": URI, "type": "Person"}
    page = page_of(record)
    assert page.title == Value("Untitled record")
    assert page.class_label == "Person"
    assert page.sections == ()


def test_names_and_identifiers() -> None:
    page = page_of(KYLIX)
    assert terms(section(page, "names")) == {
        "Primary Name": (Value("Red-figure kylix", "en"),),
        "Name": (Value("Coupe à figures rouges", "fr"),),
    }
    # Identifiers are codes, not text in a language.
    assert terms(section(page, "identifiers")) == {"Accession Number": (Value("1913.169"),)}


def test_references_link_to_the_reader_or_their_uri() -> None:
    page = page_of(KYLIX, languages={f"{AAT}300132254": "en"})
    assert section(page, "classifications").values == (
        Value("kylikes", "en", f"{AAT}300132254"),
        Value("urn:x-local:type"),
    )
    related = terms(section(page, "related"))
    member = related["Member of"][0]
    assert member.href == "/id/01937a4e-0000-7000-8000-000000000003"
    assert member.text == f"{BASE}01937a4e-0000-7000-8000-000000000003"
    assert related["Current owner"] == (Value("The Museum", None, "https://example.org/museum"),)
    assert related["Referred to by"] == (
        Value("Catalogue of Vases", None, "https://example.org/catalogue"),
    )
    assert related["x_unknown_property"] == (Value("Thing", None, "https://example.org/thing"),)


def test_production() -> None:
    production = terms(section(page_of(KYLIX), "produced_by"))
    assert production["By"] == (
        Value("Douris", None, f"/id/{ARTIST_ID}"),
        Value("Python", None, "https://example.org/python", "(potter)"),
    )
    assert production["Date"] == (Value("480 BC – 470 BC"),)
    assert production["Place"] == (Value("Athens", None, "https://example.org/athens"),)
    assert production["Technique"] == (Value("red-figure", None, f"{AAT}300069471"),)


def test_dates_show_the_display_name_and_the_period() -> None:
    record = {
        "id": URI,
        "type": "Person",
        "born": {
            "type": "Birth",
            "timespan": {
                "type": "TimeSpan",
                "identified_by": [{"type": "Name", "content": "ca. 1606"}],
                "begin_of_the_begin": "1606-07-15T00:00:00Z",
                "end_of_the_end": "1606-07-16T00:00:00Z",
            },
        },
        "died": {
            "type": "Death",
            "timespan": {"type": "TimeSpan", "begin_of_the_begin": "1669-10-04T00:00:00Z"},
        },
    }
    page = page_of(record)
    assert [found.heading for found in page.sections] == ["Birth", "Death"]
    assert terms(section(page, "born"))["Date"] == (Value("ca. 1606", note="(Jul 15, 1606)"),)
    assert terms(section(page, "died"))["Date"] == (Value("from Oct 4, 1669"),)


def test_dimensions_and_statements() -> None:
    page = page_of(KYLIX)
    assert terms(section(page, "dimensions")) == {
        "diameter": (Value("1,234.5 centimeters"),),
        "Dimension": (Value("7"),),
    }
    assert terms(section(page, "statements")) == {
        "Description": (Value("Ἐπίκτητος ἔγραψεν", "el"),),
    }


def test_numbers_follow_the_page_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    from lae.reader import i18n

    monkeypatch.setattr(i18n, "available_languages", lambda: ("de", "en"))
    page = page_of(KYLIX, chain=("de",))
    assert page.lang == "de"
    assert terms(section(page, "dimensions"))["diameter"] == (Value("1.234,5 centimeters"),)


def test_version_banner() -> None:
    old = page_of(KYLIX, version=PageVersion(2, 3, PUBLISHED))
    assert old.banner is not None
    assert old.banner.text == "You are viewing version 2 of this record, published Jan 2, 2026."
    assert (old.banner.current_href, old.banner.current_text) == (
        f"/id/{RECORD_ID}",
        "View the current version",
    )
    assert [link.href for link in old.data_links] == [
        "?version=2&format=jsonld",
        "?version=2&format=ttl",
    ]
    current = page_of(KYLIX, version=PageVersion(3, 3, PUBLISHED))
    assert current.banner is not None
    assert (current.banner.current_href, current.banner.current_text) == (
        None,
        "This is the current version.",
    )
    assert page_of(KYLIX).banner is None


def test_edit_link_only_when_signed_in() -> None:
    assert page_of(KYLIX).edit_href is None
    assert page_of(KYLIX, signed_in=True).edit_href == f"/edit/{RECORD_ID}"


def test_script_only_with_a_iiif_resource() -> None:
    assert page_of(KYLIX).script_src is None
    assert page_of(KYLIX).media is None
    with_image = dict(KYLIX)
    with_image["representation"] = [
        {
            "type": "VisualItem",
            "digitally_shown_by": [
                {
                    "type": "DigitalObject",
                    "access_point": [{"id": "https://iiif.test/image/k/full/max/0/default.jpg"}],
                }
            ],
        }
    ]
    page = page_of(with_image)
    assert page.script_src == "/reader/assets/reader.js"
    assert page.media is not None
    assert page.media.alt == "Image of Red-figure kylix"


def test_rendered_page() -> None:
    page = page_of(KYLIX, chain=("fr", "en"), languages={ARTIST: "en"}, signed_in=True)
    html = render_page(page, catalogue_for(["fr", "en"]))
    assert html.startswith('<!DOCTYPE html>\n<html lang="en" dir="ltr">\n')
    assert "<title>Coupe à figures rouges – Laertes</title>" in html
    assert '<h1 lang="fr" dir="auto">Coupe à figures rouges</h1>' in html
    assert f'<a href="/id/{ARTIST_ID}" lang="en" dir="auto">Douris</a>' in html
    assert '<span class="note">(potter)</span>' in html
    assert '<span lang="el" dir="auto">Ἐπίκτητος ἔγραψεν</span>' in html
    assert f'<a class="edit" href="/edit/{RECORD_ID}">Edit this record</a>' in html
    assert '<a href="?format=ttl" type="text/turtle">Turtle</a>' in html
    assert f'<link rel="alternate" type="{JSONLD_TYPE.replace(chr(34), "&#34;")}"' in html
    assert "<script" not in html


def test_rendered_page_escapes_record_text() -> None:
    record = {
        "id": URI,
        "type": "HumanMadeObject",
        "identified_by": [{"type": "Name", "content": '<script>alert("x")</script>'}],
        "referred_to_by": [{"type": "LinguisticObject", "content": "<b>bold</b> & co"}],
    }
    html = render_page(page_of(record), catalogue_for(["en"]))
    assert "<script>" not in html
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in html
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; co" in html


@pytest.mark.parametrize("name", sorted(str(p.relative_to(CORPUS_DIR)) for p in corpus_files()))
def test_every_corpus_record_renders(name: str) -> None:
    record = load_record(CORPUS_DIR / name)
    html = render_page(page_of(record), catalogue_for(["en"]))
    assert html.count("<h1") == 1
    assert re.search(r"<title>[^<]+ – Laertes</title>", html)
