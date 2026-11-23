"""Autocomplete pieces that need neither Postgres nor QLever: query text, ranking, merging."""

from __future__ import annotations

import json
from pathlib import Path

import pyoxigraph as ox
import pytest

from lae.api.errors import ApiError
from lae.api.records import RECORD_TYPES
from lae.search.autocomplete import (
    NOT_AT_START,
    SEARCH_CODES,
    Hit,
    build_context_query,
    build_search_query,
    class_iris,
    collect_context,
    display_order,
    life_dates,
    like_escape,
    merge_hits,
    normalize_query,
    query_words,
    rank_key,
    requested_types,
    with_locale,
    year_of,
)
from lae.sparql.terms import string_literal

ERRORS_JSON = Path(__file__).resolve().parents[3] / "frontend/public/locales/en/errors.json"
CRM = "http://www.cidoc-crm.org/cidoc-crm/"
PERSON = f"{CRM}E21_Person"


def parses(sparql: str) -> None:
    """The text is one valid SPARQL query (QLever's ``ql:`` predicates are ordinary IRIs)."""
    list(ox.Store().query(sparql))  # type: ignore[arg-type]


def test_every_code_has_an_english_message() -> None:
    messages: dict[str, str] = json.loads(ERRORS_JSON.read_text())
    assert {key for key in messages if key.startswith("LAE-SEARCH-")} == set(SEARCH_CODES)


@pytest.mark.parametrize(
    ("q", "normalized", "words"),
    [
        ("rem", "rem", ["rem"]),
        ("  Rembrandt   van ", "Rembrandt van", ["rembrandt", "van"]),
        ("van-Gogh", "van-Gogh", ["van", "gogh"]),
        ("O'Keeffe", "O'Keeffe", ["o", "keeffe"]),
        ("Aíyina", "Aíyina", ["aíyina"]),
        ("18th–19th", "18th–19th", ["18th", "19th"]),
        ('" } ; DROP ALL', '" } ; DROP ALL', ["drop", "all"]),
        ("snake_case", "snake_case", ["snake", "case"]),
        ("--", "--", []),
    ],
)
def test_query_words(q: str, normalized: str, words: list[str]) -> None:
    assert normalize_query(q) == normalized
    assert query_words(normalized) == words


def test_rank_key_prefers_labels_that_start_with_the_query_then_short_ones() -> None:
    assert rank_key("Rembrandt", "rem") == 9
    assert rank_key("REMBRANDT", "rem") == 9
    assert rank_key("Night Watch by Rembrandt", "rem") == NOT_AT_START + 24
    labels = ["Night Watch by Rembrandt", "Rembrandt van Rijn", "remains", "Rembrandt"]
    assert sorted(labels, key=lambda label: rank_key(label, "rem")) == [
        "remains",
        "Rembrandt",
        "Rembrandt van Rijn",
        "Night Watch by Rembrandt",
    ]


def test_like_escape() -> None:
    assert like_escape("100%_off\\") == "100\\%\\_off\\\\"
    assert like_escape("plain") == "plain"


def test_class_iris_cover_every_record_type() -> None:
    iris = class_iris(1)
    assert set(iris) == set(RECORD_TYPES)
    assert iris["Person"] == PERSON
    assert iris["Place"] == f"{CRM}E53_Place"
    assert iris["Type"] == f"{CRM}E55_Type"


def test_search_query_text() -> None:
    sparql = build_search_query(["rembrandt", "van"], "rembrandt van", [PERSON], 20)
    parses(sparql)
    assert sparql.count('ql:contains-word "rembrandt* van*"') == 2
    assert f"FILTER(?class IN (<{PERSON}>))" in sparql
    assert 'STRSTARTS(LCASE(STR(?lit)), "rembrandt van")' in sparql
    assert sparql.endswith("LIMIT 20")


@pytest.mark.parametrize(
    "prefix", ['" } ; DROP ALL ; "', "\\u0022 } #", "back\\slash", "new\nline", "\\U0001F600"]
)
def test_search_query_escapes_the_prefix(prefix: str) -> None:
    """The typed text stays one literal: the query still parses, and it reads back unchanged."""
    sparql = build_search_query(query_words(prefix) or ["x"], prefix, [PERSON], 5)
    parses(sparql)
    literal = string_literal(prefix)
    assert f"STRSTARTS(LCASE(STR(?lit)), {literal})" in sparql
    [row] = ox.Store().query(f"SELECT ?x WHERE {{ BIND({literal} AS ?x) }}")  # type: ignore[misc]
    assert row["x"].value == prefix


def test_search_query_needs_words() -> None:
    with pytest.raises(ValueError):
        build_search_query([], "--", [PERSON], 5)


def test_context_query() -> None:
    assert build_context_query([], [], []) is None
    sparql = build_context_query(["urn:p2", "urn:p1", "urn:p1"], ["urn:place"], ["urn:type"])
    assert sparql is not None
    parses(sparql)
    assert "VALUES ?entity { <urn:p1> <urn:p2> }" in sparql
    assert "crm:P98i_was_born" in sparql and "crm:P100i_died_in" in sparql
    assert "?entity crm:P89_falls_within ?parent" in sparql
    assert "?entity skos:broader ?parent" in sparql
    only_places = build_context_query([], ["urn:place"], [])
    assert only_places is not None and "P98i_was_born" not in only_places
    with pytest.raises(ValueError):
        build_context_query(["urn:a> } ; DROP ALL ; <urn:b"], [], [])


def test_merge_hits_ranks_both_sources_together_and_lists_each_uri_once() -> None:
    indexed = [
        Hit("urn:a", "Person", 9, "Rembrandt"),
        Hit("urn:b", "Person", NOT_AT_START + 20, "Night Watch by Rem"),
    ]
    overlay = [
        Hit("urn:new", "Person", 7, "Remarks"),
        Hit("urn:b", "Person", 12, "Rem Koolhaas"),
        Hit("urn:a", "Person", 9, "Rembrandt (renamed)"),
    ]
    merged = merge_hits(indexed, overlay, 10)
    assert [(hit.uri, hit.rank) for hit in merged] == [("urn:new", 7), ("urn:a", 9), ("urn:b", 12)]
    assert merged[1].label == "Rembrandt"  # a tie keeps QLever's hit
    assert [hit.uri for hit in merge_hits(indexed, overlay, 2)] == ["urn:new", "urn:a"]
    assert merge_hits([], [], 5) == []


def test_display_order_puts_the_most_used_first_within_prefix_matches() -> None:
    hits = [
        Hit("urn:short", "Type", 5, "Paper"),
        Hit("urn:used", "Type", 8, "Painting"),
        Hit("urn:tie", "Type", 8, "Pastels"),
        Hit("urn:word", "Type", NOT_AT_START + 9, "Oil paint"),
    ]
    usage = {"urn:used": 120, "urn:word": 900, "urn:short": 3}
    ordered = display_order(hits, usage)
    # A label starting with q still beats a better used one that only has a word starting with it.
    assert [hit.uri for hit in ordered] == ["urn:used", "urn:short", "urn:tie", "urn:word"]
    assert display_order(hits, {}) == sorted(hits, key=lambda hit: (hit.rank, hit.uri))


@pytest.mark.parametrize(
    ("value", "year"),
    [
        ("1606-07-15T00:00:00Z", "1606"),
        ("1669-12-31T23:59:59Z", "1669"),
        ("0800-01-01T00:00:00Z", "800"),
        ("-0043-03-15T00:00:00Z", "-43"),
        ("0000-01-01T00:00:00Z", "0"),
        ("12345-01-01T00:00:00Z", "12345"),
        ("not a date", None),
        (1606, None),
        (None, None),
    ],
)
def test_year_of(value: object, year: str | None) -> None:
    assert year_of(value) == year


def test_life_dates() -> None:
    born, died = "1606-07-15T00:00:00Z", "1669-10-04T23:59:59Z"
    assert life_dates([born], [died]) == "1606–1669"
    assert life_dates([born], []) == "1606–"
    assert life_dates([], [died]) == "–1669"
    assert life_dates([], []) is None
    assert life_dates(["bad"], []) is None
    # Several values (two birth records): the earliest birth and the latest death.
    assert life_dates([born, "1605-01-01T00:00:00Z"], [died, "1670-01-01T00:00:00Z"]) == (
        "1605–1670"
    )
    assert life_dates(["-0043-01-01T00:00:00Z"], ["0017-01-01T00:00:00Z"]) == "-43–17"


def test_collect_context() -> None:
    rows = [
        {"entity": "urn:p", "birth": "1606-01-01T00:00:00Z", "death": None},
        {"entity": "urn:p", "birth": "1606-01-01T00:00:00Z", "death": "1669-01-01T00:00:00Z"},
        {"entity": "urn:place", "parent": "urn:nl", "parentLabel": None},
        {"entity": "urn:place", "parent": "urn:nl", "parentLabel": "Netherlands"},
        {"entity": "urn:type", "parent": None},
        {"entity": None},
    ]
    found = collect_context(rows)
    assert found["urn:p"].births == {"1606-01-01T00:00:00Z"}
    assert found["urn:p"].deaths == {"1669-01-01T00:00:00Z"}
    assert found["urn:place"].parents == {"urn:nl": "Netherlands"}
    assert found["urn:type"].parents == {}
    assert set(found) == {"urn:p", "urn:place", "urn:type"}


def test_requested_types() -> None:
    assert requested_types(None) == sorted(RECORD_TYPES)
    assert requested_types(" ") == sorted(RECORD_TYPES)
    assert requested_types("Person, Group,,Person") == ["Person", "Group"]
    with pytest.raises(ApiError) as info:
        requested_types("Person,Name")
    assert info.value.code == "LAE-SEARCH-002"
    assert info.value.params["type"] == "Name"


def test_with_locale() -> None:
    assert with_locale(["fr", "en"], None) == ["fr", "en"]
    assert with_locale(["fr", "en"], "de-at") == ["de-AT", "fr", "en"]
    assert with_locale(["fr", "en"], "en") == ["en", "fr"]
    assert with_locale(["fr", "en"], "not a tag!") == ["fr", "en"]
