"""ADR-0028 in ``lae.rdf``: a repeated pinned ``@context`` and IRIs that need percent-encoding.

The first tests are the minimal reproductions from the report on the 32 LUX records (PR #130);
the last one runs on those records themselves once ``tests/corpus/lux/`` is present.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pyoxigraph as ox
import pytest

from lae.corpus import CORPUS_DIR
from lae.ids import derived_node_iri
from lae.rdf import (
    ExpansionError,
    InvalidIRIError,
    canonical_ntriples,
    expand_to_ntriples,
    owned_subjects,
    parse_ntriples,
    repair_iri,
    repaired_iris,
    strip_embedded_ids,
    strip_nested_contexts,
    strip_stub_attributes,
    to_turtle,
)
from lae.rdf.expand import EXPANDER_VERSION, LA_EQUIVALENT

from .node_iris import assign_node_iris

CTX = "https://linked.art/ns/v1/linked-art.json"
URI = "https://lux.collections.yale.edu/data/person/2107305b-7a4c-4af8-9d02-7a75bb1b2a4e"
CRM = "http://www.cidoc-crm.org/cidoc-crm/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
COMMONS = "https://commons.wikimedia.org/wiki/Special:Filepath/"
IMAGE = COMMONS + "Rembrandt van Rijn - Self-Portrait - Google Art Project.jpg"
IMAGE_ENCODED = (
    COMMONS + "Rembrandt%20van%20Rijn%20-%20Self-Portrait%20-%20Google%20Art%20Project.jpg"
)
PLAIN_IMAGE = "https://example.org/a.jpg"
LUX_DIR = CORPUS_DIR / "lux"


def record(nested: Any, image: str) -> dict[str, Any]:
    """The reproduction of PR #130: a ``DigitalObject`` under ``representation``."""
    shown: dict[str, Any] = {
        "type": "DigitalObject",
        "access_point": [{"id": image, "type": "DigitalObject"}],
    }
    if nested is not None:
        shown = {"@context": nested, **shown}
    return {
        "@context": CTX,
        "id": URI,
        "type": "Person",
        "_label": "x",
        "representation": [{"type": "VisualItem", "digitally_shown_by": [shown]}],
    }


def expand(doc: dict[str, Any]) -> frozenset[str]:
    return expand_to_ntriples(assign_node_iris(doc, doc["id"]), 1)


PLAIN = expand(record(None, PLAIN_IMAGE))


# --- nested @context ------------------------------------------------------------------------


@pytest.mark.parametrize("nested", [CTX, [CTX]])
def test_a_nested_context_equal_to_the_pinned_one_is_ignored(nested: Any) -> None:
    doc = record(nested, PLAIN_IMAGE)
    assert expand(doc) == PLAIN
    assert to_turtle(doc, 1) == to_turtle(record(None, PLAIN_IMAGE), 1)
    stripped = strip_stub_attributes(assign_node_iris(doc, URI), URI)
    assert "@context" not in stripped["representation"][0]["digitally_shown_by"][0]
    assert stripped["@context"] == CTX


def test_the_pinned_context_may_be_repeated_when_the_root_declares_none() -> None:
    doc = record(CTX, PLAIN_IMAGE)
    del doc["@context"]
    assert expand(doc) == PLAIN
    with_iris = assign_node_iris(doc, URI)
    # Without a root @context only the context version says which context is the pinned one.
    stripped = strip_stub_attributes(with_iris, URI, context_version=1)
    assert "@context" not in stripped["representation"][0]["digitally_shown_by"][0]
    with pytest.raises(ExpansionError, match="@context"):
        strip_stub_attributes(with_iris, URI)


@pytest.mark.parametrize(
    "nested",
    [
        "https://linked.art/ns/v1/linked-art.json#",
        "http://linked.art/ns/v1/linked-art.json",
        "https://example.org/context.json",
        [CTX, CTX],
        [CTX, {"content": "http://example.org/p"}],
        [[CTX]],
        {"@context": CTX},
        {"@vocab": "http://example.org/"},
        {},
        [],
        None,
        False,
    ],
)
def test_any_other_nested_context_is_still_rejected(nested: Any) -> None:
    doc = record(CTX, PLAIN_IMAGE)
    doc["representation"][0]["digitally_shown_by"][0]["@context"] = nested
    with pytest.raises(ExpansionError, match="carries its own @context"):
        expand(doc)
    with pytest.raises(ExpansionError, match="carries its own @context"):
        to_turtle(doc, 1)
    with pytest.raises(ExpansionError, match="carries its own @context"):
        strip_stub_attributes(assign_node_iris(doc, URI), URI)


# --- IRI repair -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("original", "encoded"),
    [
        (IMAGE, IMAGE_ENCODED),
        ("https://example.org/a b", "https://example.org/a%20b"),
        ("https://example.org/a<b>c", "https://example.org/a%3Cb%3Ec"),
        ('https://example.org/a"b', "https://example.org/a%22b"),
        ("https://example.org/{a}|b", "https://example.org/%7Ba%7D%7Cb"),
        ("https://example.org/a\\b^c`d", "https://example.org/a%5Cb%5Ec%60d"),
        ("https://example.org/a\nb\tc\rd", "https://example.org/a%0Ab%09c%0Dd"),
        ("https://example.org/a\x00b\x1fc", "https://example.org/a%00b%1Fc"),
        ("https://example.org/a b", "https://example.org/a%C2%A0b"),
        ("https://example.org/a b　c", "https://example.org/a%E2%80%A8b%E3%80%80c"),
        ("https://example.org/a b ", "https://example.org/a%20b%20"),
        ("https://example.org/a%20b c", "https://example.org/a%20b%20c"),
        ("https://example.org/é à", "https://example.org/é%20à"),
        ("urn:isbn:0 1", "urn:isbn:0%201"),
        # Syntax only: nothing is known about what a scheme allows where.
        ("https:// example.org/a.jpg", "https://%20example.org/a.jpg"),
        ("mailto:a b@example.org", "mailto:a%20b@example.org"),
    ],
)
def test_repair_iri_percent_encodes_only_what_an_iri_may_not_contain(
    original: str, encoded: str
) -> None:
    assert repair_iri(original) == encoded
    assert repair_iri(encoded) == encoded, "idempotent"
    lines = expand(record(None, original))
    assert lines == {line.replace(PLAIN_IMAGE, encoded) for line in PLAIN}
    assert parse_ntriples(canonical_ntriples(lines)) == lines
    turtle = to_turtle(record(None, original), 1)
    objects = {quad.object for quad in ox.parse(turtle, format=ox.RdfFormat.TURTLE)}
    assert ox.NamedNode(encoded) in objects


@pytest.mark.parametrize(
    "valid",
    [
        PLAIN_IMAGE,
        "https://example.org/a%20b",
        "https://example.org/a\x7fb",
        "https://example.org/a​b",
        "https://例え.jp/絵",
        "urn:uuid:0192f0c1-0000-7000-8000-000000000001",
    ],
)
def test_an_iri_that_can_be_written_is_never_changed(valid: str) -> None:
    assert repair_iri(valid) == valid
    assert repaired_iris(record(None, valid), URI) == []
    assert any(f"<{valid}> ." in line for line in expand(record(None, valid)))


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " ",
        "a b.jpg",
        "relative/a b",
        "/wiki/Special:Filepath/a b.jpg",
        "//commons.wikimedia.org/a b.jpg",
        " https://example.org/a.jpg",
        "https ://example.org/a.jpg",
        "https:/",
        "1http://example.org/a b",
        "_:b0",
    ],
)
def test_an_iri_that_cannot_be_repaired_still_raises(bad: str) -> None:
    assert repair_iri(bad) is None
    assert repaired_iris(record(None, bad), URI) == []
    with pytest.raises(ExpansionError):
        expand(record(None, bad))
    if not bad.startswith("_:"):
        with pytest.raises(InvalidIRIError):
            to_turtle(record(None, bad), 1)


def test_a_percent_sign_is_left_as_it_is() -> None:
    """No guess whether ``%`` starts an escape: one that does not is no better or worse after."""
    assert repair_iri("https://example.org/100% wool") == "https://example.org/100%%20wool"
    assert repair_iri("https://example.org/a%2Fb c") == "https://example.org/a%2Fb%20c"


def test_the_reproductions_of_pr_130() -> None:
    assert expand(record(CTX, PLAIN_IMAGE)) == PLAIN
    with_space = expand(record(None, "https://example.org/a b.jpg"))
    assert any("<https://example.org/a%20b.jpg>" in line for line in with_space)
    assert "<https://example.org/a%20b.jpg>" in to_turtle(
        record(None, "https://example.org/a b.jpg"), 1
    )
    both = expand(record(CTX, IMAGE))
    assert len(both) == len(PLAIN)
    assert sum(f"<{IMAGE_ENCODED}> ." in line for line in both) == 1


def test_the_stored_json_is_not_changed() -> None:
    doc = assign_node_iris(record(CTX, IMAGE), URI)
    before = copy.deepcopy(doc)
    expand_to_ntriples(doc, 1)
    to_turtle(doc, 1)
    repaired_iris(doc, URI)
    stripped = strip_stub_attributes(doc, URI)
    assert doc == before
    # What the SHACL index walks next to the document still has the id as it is in the JSON-LD.
    shown = stripped["representation"][0]["digitally_shown_by"][0]
    assert shown["access_point"] == [{"id": IMAGE}]


def test_repaired_iris_gives_the_path_of_each_id_expansion_writes() -> None:
    doc = record(None, IMAGE)
    doc["classified_as"] = [
        {
            "id": "http://vocab.getty.edu/aat/300 1",
            "type": "Type",
            # Inside a reference: dropped by expansion, so not reported.
            "classified_as": [{"id": "http://vocab.getty.edu/aat/300 2", "type": "Type"}],
        }
    ]
    doc["identified_by"] = [
        {
            "id": "https://museum.example.org/name 1",
            "type": "Name",
            "content": "x",
            "language": [{"id": "http://vocab.getty.edu/aat/300 3", "type": "Language"}],
        }
    ]
    assert repaired_iris(assign_node_iris(doc, URI), URI) == [
        (
            ("representation", 0, "digitally_shown_by", 0, "access_point", 0, "id"),
            IMAGE,
            IMAGE_ENCODED,
        ),
        (
            ("classified_as", 0, "id"),
            "http://vocab.getty.edu/aat/300 1",
            "http://vocab.getty.edu/aat/300%201",
        ),
        (
            ("identified_by", 0, "id"),
            "https://museum.example.org/name 1",
            "https://museum.example.org/name%201",
        ),
        (
            ("identified_by", 0, "language", 0, "id"),
            "http://vocab.getty.edu/aat/300 3",
            "http://vocab.getty.edu/aat/300%203",
        ),
    ]


def test_an_embedded_node_whose_own_id_needs_repair() -> None:
    """ADR-0021 with ADR-0028: the derived IRI comes from the id as stored, the link is encoded."""
    original = "https://museum.example.org/name 1"
    doc = {
        "@context": CTX,
        "id": URI,
        "type": "Person",
        "identified_by": [{"id": original, "type": "Name", "content": "x"}],
    }
    derived = derived_node_iri(URI, original)
    assert expand_to_ntriples(doc, 1) == {
        f"<{URI}> <{RDF_TYPE}> <{CRM}E21_Person> .",
        f"<{URI}> <{CRM}P1_is_identified_by> <{derived}> .",
        f"<{derived}> <{RDF_TYPE}> <{CRM}E33_E41_Linguistic_Appellation> .",
        f'<{derived}> <{CRM}P190_has_symbolic_content> "x" .',
        f"<{derived}> <{LA_EQUIVALENT}> <https://museum.example.org/name%201> .",
    }
    assert derived in owned_subjects(doc, URI)


def test_only_ids_are_repaired() -> None:
    """A bare string, a class and the record URI are not data to repair: they still raise."""
    doc = record(None, PLAIN_IMAGE)
    doc["representation"][0]["digitally_shown_by"][0]["access_point"] = ["https://example.org/a b"]
    with pytest.raises(InvalidIRIError):
        expand(doc)
    doc = record(None, PLAIN_IMAGE)
    doc["representation"][0]["type"] = "https://example.org/Visual Item"
    with pytest.raises(InvalidIRIError):
        expand(doc)
    with pytest.raises(InvalidIRIError):
        expand_to_ntriples({"@context": CTX, "id": URI + " 1", "type": "Person"}, 1)
    # Nor in Turtle, where no caller has checked the record URI first.
    with pytest.raises(InvalidIRIError):
        to_turtle({"@context": CTX, "id": URI + " 1", "type": "Person"}, 1)


def test_a_repaired_iri_is_never_a_subject() -> None:
    """``a b`` and ``a%20b`` are one IRI in RDF, but only ever as an object (ADR-0021 ownership).

    Two embedded nodes whose ids differ only in the encoding stay two nodes of the record, a
    reference spelled either way is one object, and an id that encodes to the record URI itself
    neither becomes the root nor adds a triple to it.
    """
    spaced, encoded = "https://museum.example.org/name/a b", "https://museum.example.org/name/a%20b"
    doc: dict[str, Any] = {
        "@context": CTX,
        "id": URI,
        "type": "Person",
        "identified_by": [
            {"id": spaced, "type": "Name", "content": "one"},
            {"id": encoded, "type": "Name", "content": "two"},
        ],
        "classified_as": [{"id": spaced, "type": "Type"}, {"id": encoded, "type": "Type"}],
    }
    lines = expand_to_ntriples(doc, 1)
    first, second = derived_node_iri(URI, spaced), derived_node_iri(URI, encoded)
    assert first != second
    assert {line.split(" ", 1)[0] for line in lines} == {f"<{URI}>", f"<{first}>", f"<{second}>"}
    for node, content in ((first, "one"), (second, "two")):
        assert f'<{node}> <{CRM}P190_has_symbolic_content> "{content}" .' in lines
        assert f"<{node}> <{LA_EQUIVALENT}> <{encoded}> ." in lines
    assert {first, second} <= owned_subjects(doc, URI)
    assert not any(line.startswith(f"<{encoded}>") for line in lines)

    record_uri = "https://museum.example.org/person/a%20b"
    alias = "https://museum.example.org/person/a b"
    own: dict[str, Any] = {"@context": CTX, "id": record_uri, "type": "Person", "_label": "x"}
    plain = expand_to_ntriples(own, 1)
    named = {"id": alias, "type": "Name", "content": "not the record's"}
    lines = expand_to_ntriples({**own, "identified_by": [named]}, 1)
    node = derived_node_iri(record_uri, alias)
    assert {line for line in lines if line.startswith(f"<{record_uri}> ")} == plain | {
        f"<{record_uri}> <{CRM}P1_is_identified_by> <{node}> ."
    }
    reference = {"id": alias, "type": "Person", "_label": "ignored"}
    assert expand_to_ntriples({**own, "equivalent": [reference]}, 1) == plain | {
        f"<{record_uri}> <{LA_EQUIVALENT}> <{record_uri}> ."
    }


def test_the_expander_version_says_that_refused_inputs_now_expand() -> None:
    """ADR-0007: a record version 1 refused has no triples in an index version 1 built."""
    assert EXPANDER_VERSION == 2


@pytest.mark.parametrize(
    "bad",
    ["https://example.org/100%wool", "https://example.org/a[1]", "https://example.org/a\x7fb"],
)
def test_turtle_refuses_an_iri_only_ntriples_can_hold_with_the_expansion_error(bad: str) -> None:
    """pyoxigraph is stricter than N-Triples; its ``ValueError`` must not escape as such."""
    doc = record(None, bad)
    assert any(f"<{bad}> ." in line for line in expand(doc))
    with pytest.raises(InvalidIRIError, match="Turtle"):
        to_turtle(doc, 1)


# --- strip_embedded_ids ---------------------------------------------------------------------


def test_strip_embedded_ids_follows_the_reference_embedded_split() -> None:
    node = URI + "/node/0192f0c1-0000-7000-8000-0000000000a1"
    doc: dict[str, Any] = {
        "@context": CTX,
        "id": URI,
        "type": "Person",
        "identified_by": [
            {"id": "https://museum.example.org/name/1", "type": "Name", "content": "x"},
            {"id": node, "type": "Name", "content": "y"},
            {"@id": "https://museum.example.org/name/2", "type": "Name", "content": "z"},
        ],
        "classified_as": [
            {"id": "http://vocab.getty.edu/aat/300025103", "type": "Type", "_label": "t"},
            {
                "id": "http://vocab.getty.edu/aat/300025104",
                "type": "Type",
                "classified_as": [{"id": "http://vocab.getty.edu/aat/300435443", "type": "Type"}],
            },
        ],
        "born": {
            "id": "https://museum.example.org/birth/1",
            "type": "Birth",
            "timespan": {
                "id": "https://museum.example.org/ts/1",
                "type": "TimeSpan",
                "begin_of_the_begin": "1606-07-15T00:00:00Z",
            },
            "took_place_at": [
                {"id": "https://museum.example.org/place/1", "type": "Place", "_label": "Leiden"}
            ],
        },
        "referred_to_by": [
            {"id": "https://museum.example.org/text/1", "type": "LinguisticObject"},
            {"id": "https://museum.example.org/text/2", "type": "LinguisticObject", "content": "c"},
            {"id": "_:b0", "type": "LinguisticObject", "content": "c"},
        ],
    }
    before = copy.deepcopy(doc)
    stripped = strip_embedded_ids(doc, URI)
    assert doc == before
    expected = copy.deepcopy(doc)
    del expected["identified_by"][0]["id"]
    del expected["identified_by"][2]["@id"]
    del expected["born"]["id"]
    del expected["born"]["timespan"]["id"]
    del expected["referred_to_by"][1]["id"]
    assert stripped == expected


def test_strip_embedded_ids_keeps_what_the_schema_reads_as_a_reference() -> None:
    """The schema's ``*Ref`` definitions allow ``equivalent`` and require ``id``.

    Expansion treats such an object of a class that is not in ``ENTITY_TYPES`` as an embedded
    node (ADR-0021), but stripping its ``id`` would make a schema-valid reference fail.
    """
    same = [{"id": "https://other.example.org/image/1", "type": "VisualItem"}]
    doc: dict[str, Any] = {
        "@context": CTX,
        "id": URI,
        "type": "Person",
        "representation": [
            {"id": "https://museum.example.org/image/1", "type": "VisualItem", "equivalent": same},
            {
                "id": "https://museum.example.org/image/2",
                "type": "VisualItem",
                "_label": "x",
                "classified_as": [{"id": "http://vocab.getty.edu/aat/300015636", "type": "Type"}],
                "equivalent": same,
            },
            {
                "id": "https://museum.example.org/image/3",
                "type": "VisualItem",
                "equivalent": same,
                "digitally_shown_by": [{"id": "https://museum.example.org/d/1", "format": "x"}],
            },
        ],
        "identified_by": [
            {"id": "https://museum.example.org/name/1", "type": "Name", "equivalent": same}
        ],
    }
    expected = copy.deepcopy(doc)
    del expected["representation"][2]["id"]
    del expected["representation"][2]["digitally_shown_by"][0]["id"]
    del expected["identified_by"][0]["id"]
    assert strip_embedded_ids(doc, URI) == expected


def test_strip_embedded_ids_judges_what_is_inside_a_reference_too() -> None:
    """Expansion drops what a reference carries; the schema reads it, with the same split."""
    meta = {"id": "http://vocab.getty.edu/aat/300435443", "type": "Type", "_label": "meta"}
    name = {"id": "https://museum.example.org/name/1", "type": "Name", "content": "x"}
    doc: dict[str, Any] = {
        "@context": CTX,
        "id": URI,
        "type": "Person",
        "classified_as": [
            {
                "id": "http://vocab.getty.edu/aat/300025103",
                "type": "Type",
                "classified_as": [meta],
                "identified_by": [name],
            }
        ],
    }
    expected = copy.deepcopy(doc)
    del expected["classified_as"][0]["identified_by"][0]["id"]
    assert strip_embedded_ids(doc, URI) == expected
    # In RDF the reference is the linking triple and nothing else.
    assert expand_to_ntriples(doc, 1) == {
        f"<{URI}> <{RDF_TYPE}> <{CRM}E21_Person> .",
        f"<{URI}> <{CRM}P2_has_type> <http://vocab.getty.edu/aat/300025103> .",
    }


# --- strip_nested_contexts ------------------------------------------------------------------


def test_strip_nested_contexts_drops_only_repeats_of_the_pinned_context() -> None:
    other = "https://example.org/context.json"
    doc: dict[str, Any] = {
        "@context": CTX,
        "id": URI,
        "type": "Person",
        "identified_by": [
            {"@context": CTX, "type": "Name", "content": "a"},
            {"@context": [CTX], "type": "Name", "content": "b"},
            {"@context": other, "type": "Name", "content": "c"},
            {"@context": [CTX, CTX], "type": "Name", "content": "d"},
            {"@context": {"content": "http://example.org/p"}, "type": "Name", "content": "e"},
            {"@context": None, "type": "Name", "content": "f"},
        ],
        "classified_as": [{"@context": CTX, "id": "http://vocab.getty.edu/aat/1", "type": "Type"}],
    }
    before = copy.deepcopy(doc)
    expected = copy.deepcopy(doc)
    del expected["identified_by"][0]["@context"]
    del expected["identified_by"][1]["@context"]
    del expected["classified_as"][0]["@context"]
    assert strip_nested_contexts(doc) == expected
    assert strip_nested_contexts(doc, context_version=1) == expected
    assert doc == before
    # Without a version, only a root that declares a pinned context says which one is pinned.
    for root in (other, None, [CTX], {"x": "http://example.org/x"}):
        unpinned = {**doc, "@context": root}
        assert strip_nested_contexts(unpinned) == unpinned
        assert strip_nested_contexts(unpinned, context_version=1) == {**expected, "@context": root}
    with pytest.raises(ExpansionError):
        strip_nested_contexts(doc, context_version=99)


# --- the LUX records ------------------------------------------------------------------------


def lux_records() -> list[tuple[str, dict[str, Any]]]:
    return [(path.name, json.loads(path.read_bytes())) for path in sorted(LUX_DIR.glob("*.json"))]


def nested_contexts(value: Any, *, root: bool = True) -> int:
    if isinstance(value, list):
        return sum(nested_contexts(item, root=False) for item in value)
    if isinstance(value, dict):
        return sum(
            (not root) if key == "@context" else nested_contexts(item, root=False)
            for key, item in value.items()
        )
    return 0


def test_every_lux_record_expands_and_serializes() -> None:
    """The 32 LUX records of PR #130: 7 of them raised before ADR-0028."""
    records = lux_records()
    if not records:
        pytest.skip("tests/corpus/lux/ is not in this checkout (it arrives with PR #130)")
    repaired = 0
    nested = 0
    for name, raw in records:
        uri = raw["id"]
        doc = assign_node_iris(raw, uri)
        lines = expand_to_ntriples(doc, 1)
        assert lines == expand_to_ntriples(copy.deepcopy(doc), 1), name
        subjects = {line.split(" ", 1)[0] for line in lines}
        assert all(s == f"<{uri}>" or s.startswith(f"<{uri}/node/") for s in subjects), name
        assert list(ox.parse(to_turtle(raw, 1), format=ox.RdfFormat.TURTLE)), name
        for _, original, encoded in repaired_iris(doc, uri):
            repaired += 1
            assert " " in original and encoded == original.replace(" ", "%20"), name
            assert any(f"<{encoded}> ." in line for line in lines), name
        nested += nested_contexts(raw)
    assert len(records) == 32
    assert repaired == 9
    assert nested == 11
