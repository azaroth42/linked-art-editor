"""ADR-0028 in ``lae.validation``: embedded-node ids, repaired IRIs, and the LUX records."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from lae.corpus import CORPUS_DIR, load_record
from lae.validation import (
    ReferenceFacts,
    all_codes,
    validate_document,
    validate_schema,
    validate_shacl,
)
from lae.validation.expansion import RDF_CODES, RDF_IRI_REPAIRED, iri_repair_warnings
from tests.validation.conftest import base, found, with_node_iris

CTX = "https://linked.art/ns/v1/linked-art.json"
LUX_DIR = CORPUS_DIR / "lux"
IMAGE = "https://commons.wikimedia.org/wiki/Special:Filepath/Self Portrait.jpg"
IMAGE_ENCODED = "https://commons.wikimedia.org/wiki/Special:Filepath/Self%20Portrait.jpg"
MATERIAL = "http://vocab.getty.edu/aat/300 15050"


def with_representation(image: str, nested: Any = None) -> dict[str, Any]:
    """The base object with the ``representation`` of the LUX records (PR #130)."""
    shown: dict[str, Any] = {
        "type": "DigitalObject",
        "access_point": [{"id": image, "type": "DigitalObject"}],
    }
    if nested is not None:
        shown["@context"] = nested
    doc = base()
    doc["representation"] = [{"type": "VisualItem", "digitally_shown_by": [shown]}]
    return doc


# --- ids of embedded nodes ------------------------------------------------------------------


def test_an_id_on_an_embedded_name_is_ignored_by_the_schema() -> None:
    """``docs/questions.md``, Step 1.9, item 2: the ``Name`` of ``linkedart/object-3.json``."""
    doc = base()
    doc["identified_by"][0] = {
        "id": "https://linked.art/example/Name/0",
        "type": "Name",
        "content": "Hacha",
    }
    before = copy.deepcopy(doc)
    assert validate_schema(doc) == []
    assert validate_document(doc).items == []
    assert doc == before


@pytest.mark.parametrize(
    "pointer",
    ["/identified_by/0", "/identified_by/2", "/dimension/0", "/produced_by/timespan"],
)
def test_an_id_on_any_embedded_class_is_ignored(pointer: str) -> None:
    """``Name``, ``Identifier``, ``Dimension``, ``TimeSpan``: the schema allows ``id`` on none."""
    doc = base("HumanMadeObject", [pointer + "/id", "https://museum.example.org/node/1"])
    assert validate_schema(doc) == []
    assert validate_schema(with_node_iris(doc)) == []
    assert validate_document(doc).ok


def test_a_reference_keeps_its_id() -> None:
    doc = base()
    del doc["made_of"][0]["id"]
    assert [item.code for item in validate_schema(doc)] == ["LAE-SCHEMA-required"]
    # A reference is not turned into an embedded node by what else it carries (ADR-0021).
    doc = base()
    doc["part_of"]["identified_by"] = [{"type": "Name", "content": "x"}]
    (item,) = validate_schema(doc)
    assert (item.code, item.path, item.params["extra"]) == (
        "LAE-SCHEMA-additionalProperties",
        "/part_of",
        ["identified_by"],
    )


def test_a_reference_with_equivalent_keeps_the_id_the_schema_requires() -> None:
    """``core.json`` allows ``equivalent`` on a reference; ADR-0021 would call it embedded."""
    same = [{"id": "https://other.example.org/image/1", "type": "VisualItem"}]
    doc = base()
    doc["shows"] = [
        {"id": "https://museum.example.org/image/1", "type": "VisualItem", "equivalent": same}
    ]
    assert validate_schema(doc) == []
    del doc["shows"][0]["id"]
    assert [(item.code, item.path) for item in validate_schema(doc)] == [
        ("LAE-SCHEMA-required", "/shows/0")
    ]


def test_an_id_on_a_statement_or_an_embedded_work_is_ignored() -> None:
    """Classes that can be records too are embedded when they carry content (ADR-0021)."""
    doc = base()
    doc["referred_to_by"] = [
        {
            "id": "https://museum.example.org/statement/1",
            "type": "LinguisticObject",
            "content": "Oil on canvas",
        }
    ]
    doc["representation"] = [
        {
            "id": "https://museum.example.org/image/1",
            "type": "VisualItem",
            "digitally_shown_by": [
                {
                    "id": "https://museum.example.org/digital/1",
                    "type": "DigitalObject",
                    "access_point": [{"id": "https://example.org/a.jpg", "type": "DigitalObject"}],
                }
            ],
        }
    ]
    assert validate_schema(doc) == []


def test_an_id_on_a_name_inside_a_concept_reference_is_ignored() -> None:
    """Each object is judged alone, also inside a reference: its meta-type keeps its id."""
    doc = base()
    doc["classified_as"] = [
        {
            "id": "http://vocab.getty.edu/aat/300033618",
            "type": "Type",
            "_label": "Painting",
            "classified_as": [
                {"id": "http://vocab.getty.edu/aat/300435443", "type": "Type", "_label": "m"}
            ],
            "identified_by": [
                {"id": "https://museum.example.org/name/9", "type": "Name", "content": "Painting"}
            ],
        }
    ]
    assert validate_schema(doc) == []
    del doc["classified_as"][0]["classified_as"][0]["id"]
    assert validate_schema(doc) != []


def test_an_error_inside_an_embedded_node_with_an_id_keeps_its_path() -> None:
    doc = base("HumanMadeObject", ["/identified_by/1/id", "https://museum.example.org/name/1"])
    doc["identified_by"][1]["content"] = 7
    causes = [item.params.get("cause") for item in validate_schema(doc)]
    assert causes == [{"code": "LAE-SCHEMA-type", "path": "/identified_by/1/content"}]


def test_the_corpus_names_with_their_own_ids_no_longer_fail_for_them() -> None:
    """The three files of Step 1.9, item 2 fail for other reasons only (KNOWN_FAILURES.md)."""
    remaining = {
        name: [(item.code, item.path) for item in validate_schema(load_record(CORPUS_DIR / name))]
        for name in (
            "linkedart/object-3.json",
            "linkedart/object-nightwatch-43.json",
            "linkedart/person-rembrandt-4.json",
        )
    }
    assert remaining == {
        # No top-level _label.
        "linkedart/object-3.json": [("LAE-SCHEMA-required", "")],
        # A Name without content.
        "linkedart/object-nightwatch-43.json": [
            ("LAE-SCHEMA-anyOf", "/produced_by/timespan/identified_by/0")
        ],
        # A Name part without content.
        "linkedart/person-rembrandt-4.json": [("LAE-SCHEMA-anyOf", "/identified_by/0")],
    }


# --- IRIs that RDF percent-encodes ----------------------------------------------------------


def test_a_repairable_iri_is_a_warning_that_does_not_block_publishing() -> None:
    doc = with_representation(IMAGE)
    before = copy.deepcopy(doc)
    result = validate_document(doc)
    assert result.ok
    (item,) = result.items
    assert (item.code, item.severity) == (RDF_IRI_REPAIRED, "warning")
    assert item.path == "/representation/0/digitally_shown_by/0/access_point/0/id"
    assert item.params == {"original": IMAGE, "encoded": IMAGE_ENCODED}
    assert item.message_en.endswith(IMAGE_ENCODED)
    assert doc == before, "the stored JSON keeps the id as written"
    assert validate_document(with_node_iris(doc)).items == result.items


def test_the_code_is_registered() -> None:
    assert RDF_IRI_REPAIRED == "LAE-RDF-006"
    assert RDF_IRI_REPAIRED in RDF_CODES
    assert RDF_IRI_REPAIRED in all_codes()


@pytest.mark.parametrize("bad", ["Self Portrait.jpg", "/wiki/a b.jpg", " https://example.org/a"])
def test_an_iri_that_cannot_be_repaired_is_still_a_schema_violation(bad: str) -> None:
    result = validate_document(with_representation(bad))
    assert not result.ok
    assert RDF_IRI_REPAIRED not in {item.code for item in result.items}
    causes = [item.params.get("cause") for item in result.items]
    path = "/representation/0/digitally_shown_by/0/access_point/0/id"
    assert {"code": "LAE-SCHEMA-format", "path": path} in causes or (
        "LAE-SCHEMA-format",
        "violation",
        path,
    ) in found(result.items)


def test_a_repaired_reference_is_still_checked_by_shacl() -> None:
    """The registry knows the reference by its id in the JSON-LD, RDF by the encoded IRI."""
    doc = base()
    doc["made_of"] = [{"id": MATERIAL, "type": "Material"}]
    assert found(validate_document(doc).items) == [("LAE-RDF-006", "warning", "/made_of/0/id")]
    wrong = ReferenceFacts({MATERIAL: "Place"})
    assert found(validate_document(doc, None, wrong).items) == [
        ("LAE-RDF-006", "warning", "/made_of/0/id"),
        ("LAE-SHACL-006", "violation", "/made_of/0"),
    ]


def test_no_warning_for_an_id_inside_a_reference_or_for_a_schema_invalid_record() -> None:
    doc = base()
    doc["made_of"][0]["classified_as"] = [{"id": MATERIAL, "type": "Type"}]
    assert iri_repair_warnings(doc) == []
    # Schema violations end the run before the warning, as they do for the rules and SHACL.
    doc = with_representation(IMAGE)
    del doc["_label"]
    assert {item.code for item in validate_document(doc).items} == {"LAE-SCHEMA-required"}
    assert iri_repair_warnings([doc]) == []


# --- nested @context ------------------------------------------------------------------------


@pytest.mark.parametrize("nested", [CTX, [CTX]])
def test_a_repeated_pinned_context_does_not_stop_shacl(nested: Any) -> None:
    doc = with_representation("https://example.org/a.jpg", nested)
    assert validate_document(doc).items == []
    # SHACL alone, on a document whose root declares no context (the schema requires one).
    del doc["@context"]
    assert validate_shacl(with_node_iris(doc), doc["id"], {}, context_version=1) == []


def test_another_nested_context_is_an_expansion_violation() -> None:
    doc = with_representation("https://example.org/a.jpg", {"content": "http://example.org/p"})
    assert found(validate_document(doc).items) == [("LAE-RDF-005", "violation", "")]


@pytest.mark.parametrize("nested", [CTX, [CTX]])
def test_the_schema_ignores_a_repeated_pinned_context_where_it_allows_none(nested: Any) -> None:
    """A ``Name`` and a reference: the schema has no ``@context`` on either (ADR-0028)."""
    doc = base()
    doc["identified_by"][0]["@context"] = nested
    doc["made_of"][0]["@context"] = nested
    before = copy.deepcopy(doc)
    assert validate_schema(doc) == []
    assert validate_schema(doc, context_version=1) == []
    assert validate_document(doc).items == validate_document(base()).items
    assert doc == before


@pytest.mark.parametrize(
    "nested", ["https://example.org/context.json", {"content": "http://example.org/p"}, [], None]
)
def test_the_schema_still_reports_any_other_nested_context(nested: Any) -> None:
    doc = base()
    doc["identified_by"][0]["@context"] = nested
    (item,) = validate_schema(doc)
    assert item.path == "/identified_by/0"
    assert not validate_document(doc).ok
    doc = base()
    doc["made_of"][0]["@context"] = nested
    (item,) = validate_schema(doc)
    assert (item.code, item.path) == ("LAE-SCHEMA-additionalProperties", "/made_of/0")


def test_a_record_uri_that_needs_encoding_is_refused_at_its_id() -> None:
    """The ``uri`` format lets it through; the record URI is never repaired, so RDF refuses it."""
    doc = base()
    doc["id"] = doc["id"] + " 1"
    assert validate_schema(doc) == []
    assert found(validate_document(doc).items) == [("LAE-RDF-003", "violation", "/id")]


# --- the LUX records ------------------------------------------------------------------------


def without_lux_keys(value: Any) -> Any:
    """Drop LUX's own ``_links`` and the like (every key with ``_`` except ``_label``)."""
    if isinstance(value, list):
        return [without_lux_keys(item) for item in value]
    if isinstance(value, dict):
        return {
            key: without_lux_keys(item)
            for key, item in value.items()
            if key == "_label" or not key.startswith("_")
        }
    return value


def test_lux_records_validate_without_an_engine_failure() -> None:
    """The 32 LUX records of PR #130, among them a BCE-dated ``HumanMadeObject``.

    As fetched, every one fails the schema for LUX's ``_links``. Without LUX's own keys 30 are
    publishable (one of them, a ``Period`` whose ``equivalent`` is a ``Period``, since ADR-0030
    §6); none makes expansion or pyshacl fail (the BCE tetradrachm crashed the Spike S4
    harness, whose shapes compared ``xsd:dateTime`` literals; the pipeline compares dates in
    Python).
    """
    paths = sorted(LUX_DIR.glob("*.json"))
    if not paths:
        pytest.skip("tests/corpus/lux/ is not in this checkout (it arrives with PR #130)")
    ok: list[str] = []
    codes: dict[str, int] = {}
    for path in paths:
        raw = json.loads(path.read_bytes())
        as_fetched = validate_document(raw)
        assert not as_fetched.ok
        assert {item.code.rsplit("-", 1)[0] for item in as_fetched.items} == {"LAE-SCHEMA"}
        result = validate_document(without_lux_keys(raw))
        for item in result.items:
            assert not item.code.startswith(("LAE-RDF-00", "LAE-SHACL-000")) or (
                item.code == RDF_IRI_REPAIRED
            ), (path.name, item)
            codes[item.code] = codes.get(item.code, 0) + 1
        if result.ok:
            ok.append(path.name)
    assert len(paths) == 32
    assert len(ok) == 30
    assert "object-c1b6648b-caf8-4a1f-b28d-43ce8c83a852.json" in ok, "the BCE tetradrachm"
    assert codes == {
        "LAE-RDF-006": 8,
        "LAE-SCHEMA-additionalProperties": 2,
        "LAE-SCHEMA-anyOf": 2,
        "LAE-SHACL-002": 3,
        "LAE-SHACL-014": 1,
    }
