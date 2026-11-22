"""SHACL validation and the mapping of results to JSON paths (``lae.validation.shacl``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog
from rdflib import Graph

from lae.ids import derived_node_iri
from lae.validation import shacl
from lae.validation.shacl import (
    LAE,
    SH,
    ShapesError,
    all_shape_codes,
    build_index,
    class_iri,
    load_shapes,
    shape_codes,
    shapes_graph,
    validate_shacl,
)
from tests.validation.conftest import SHAPE_CASES, apply, base, found, with_node_iris

CRM = "http://www.cidoc-crm.org/cidoc-crm/"
RETIRED_CODES = {"LAE-SHACL-009", "LAE-SHACL-010"}
PREFIXES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix crm: <http://www.cidoc-crm.org/cidoc-crm/> .
@prefix lae: <urn:lae:ns:> .
@prefix laes: <urn:lae:shape:> .
"""


def run(doc: dict[str, Any], referenced_types: dict[str, str] | None = None) -> list[Any]:
    return validate_shacl(with_node_iris(doc), doc["id"], referenced_types or {})


# --- shapes ---------------------------------------------------------------------------------


def test_every_targeted_shape_has_one_unique_code() -> None:
    codes = shape_codes(shapes_graph())
    assert len(codes) == 19
    assert len(set(codes.values())) == len(codes)
    assert set(codes.values()) == {f"LAE-SHACL-{n:03d}" for n in (*range(1, 9), *range(11, 22))}
    assert not RETIRED_CODES & all_shape_codes()
    assert "LAE-SHACL-000" in all_shape_codes()


def test_shapes_have_no_datetime_checks() -> None:
    shapes = shapes_graph()
    xsd_datetime = "http://www.w3.org/2001/XMLSchema#dateTime"
    assert not [o for o in shapes.objects(None, SH.datatype) if str(o) == xsd_datetime]
    assert not list(shapes.subject_objects(SH.lessThanOrEquals))
    assert not list(shapes.subject_objects(SH.lessThan))


def test_every_shape_has_an_english_message() -> None:
    shapes = shapes_graph()
    for shape in shape_codes(shapes):
        messages = list(shapes.objects(shape, SH.message))
        for constraint in shapes.objects(shape, SH.sparql):
            messages.extend(shapes.objects(constraint, SH.message))
        assert [m for m in messages if getattr(m, "language", None) == "en"], shape


def test_cases_cover_every_code() -> None:
    expected = {code for case in SHAPE_CASES["cases"] for code, _, _ in case["expect"]}
    assert expected == all_shape_codes() - {"LAE-SHACL-000"}


@pytest.mark.parametrize(
    ("turtle", "message"),
    [
        ("laes:a sh:targetClass crm:E21_Person ; sh:path crm:P1 ; sh:minCount 1 .", "exactly one"),
        (
            'laes:a sh:targetClass crm:E21_Person ; lae:code "LAE-SHACL-900", "LAE-SHACL-901" .',
            "exactly one",
        ),
        ('laes:a sh:targetClass crm:E21_Person ; lae:code "SHACL-1" .', "exactly one"),
        (
            'laes:a sh:targetClass crm:E21_Person ; lae:code "LAE-SHACL-900" .\n'
            'laes:b sh:targetObjectsOf crm:P1 ; lae:code "LAE-SHACL-900" .',
            "already used",
        ),
        ('laes:a sh:targetClass crm:E21_Person ; lae:code "LAE-SHACL-000" .', "already used"),
        ('[] sh:targetClass crm:E21_Person ; lae:code "LAE-SHACL-900" .', "blank node"),
    ],
)
def test_shapes_that_break_the_code_rules_are_refused(
    turtle: str, message: str, tmp_path: Path
) -> None:
    (tmp_path / "bad.ttl").write_text(PREFIXES + turtle)
    with pytest.raises(ShapesError, match=message):
        load_shapes(tmp_path)


def test_untargeted_shapes_need_no_code(tmp_path: Path) -> None:
    (tmp_path / "ok.ttl").write_text(
        PREFIXES + 'laes:a sh:targetClass crm:E21_Person ; lae:code "LAE-SHACL-900" ;'
        " sh:or ( [ sh:class crm:E21_Person ] ) ."
    )
    assert isinstance(load_shapes(tmp_path), Graph)


# --- cases ----------------------------------------------------------------------------------


@pytest.mark.parametrize("case", SHAPE_CASES["cases"], ids=lambda case: case["name"])
def test_case(case: dict[str, Any]) -> None:
    doc = apply(SHAPE_CASES["bases"][case["base"]], case["set"])
    items = run(doc, case.get("referenced_types"))
    assert found(items) == sorted(tuple(expected) for expected in case["expect"])


def test_severity_comes_from_the_result_not_from_conforms() -> None:
    items = run(base("HumanMadeObject", ["/identified_by", None], ["/dimension/0/unit", None]))
    assert {item.severity for item in items} == {"warning"}
    assert len(items) == 2


def test_message_and_value_params() -> None:
    doc = base(
        "HumanMadeObject",
        ["/made_of/0/type", "Type"],
        ["/dimension/0/value", "50.5 cm"],
    )
    by_code = {item.code: item for item in run(doc)}
    assert by_code["LAE-SHACL-006"].params == {"value": "http://vocab.getty.edu/aat/300015050"}
    assert by_code["LAE-SHACL-006"].message_en == "made_of must refer to a Material."
    assert by_code["LAE-SHACL-007"].params == {"value": "50.5 cm"}


def test_internal_node_iris_never_appear_in_params() -> None:
    """Not as the focus node of a SPARQL result, and not as a value that is a node."""
    doc = base(
        "HumanMadeObject",
        ["/identified_by", None],
        ["/produced_by/type", "Activity"],
        ["/produced_by/timespan/end_of_the_end", None],
        ["/dimension/0/unit", None],
    )
    items = run(doc)
    assert {item.code for item in items} == {
        "LAE-SHACL-001",
        "LAE-SHACL-004",
        "LAE-SHACL-008",
        "LAE-SHACL-014",
    }
    assert all(item.params == {} for item in items), [item.params for item in items]
    assert "/node/" not in "".join(item.model_dump_json() for item in items)
    assert doc["id"] not in "".join(item.model_dump_json() for item in items)


def test_results_are_the_same_on_every_run() -> None:
    doc = base("HumanMadeObject", ["/identified_by", None], ["/made_of/0/type", "Type"])
    assert found(run(doc)) == found(run(doc))


# --- data graph -----------------------------------------------------------------------------


def test_class_iri_comes_from_the_pinned_context() -> None:
    assert class_iri("Material", 1) == CRM + "E57_Material"
    assert class_iri("HumanMadeObject", 1) == CRM + "E22_Human-Made_Object"
    assert class_iri("Currency", 1) == CRM + "E98_Currency"
    assert class_iri("NoSuchClass", 1) is None


def test_entity_labels_type_wins_over_the_declared_type() -> None:
    material = "http://vocab.getty.edu/aat/300015050"
    assert found(run(base(), {material: "Type"})) == [("LAE-SHACL-006", "violation", "/made_of/0")]
    declared_wrong = base("HumanMadeObject", ["/made_of/0/type", "Type"])
    assert run(declared_wrong, {material: "Material"}) == []
    # A class name the context does not know types nothing, so the constraint fails.
    assert found(run(base(), {material: "NoSuchClass"})) == [
        ("LAE-SHACL-006", "violation", "/made_of/0")
    ]


def test_a_referenced_entity_is_never_a_focus_node() -> None:
    """The parent object is a HumanMadeObject in the graph, but its names are not ours to judge."""
    doc = base()
    doc["part_of"] = {"id": "https://example.org/object/0", "type": "HumanMadeObject"}
    doc["made_of"][0]["classified_as"] = [{"id": "https://example.org/x", "type": "Type"}]
    assert run(doc) == []


def test_an_embedded_object_is_a_focus_node() -> None:
    doc = base()
    doc["part"] = [{"type": "HumanMadeObject", "_label": "Frame", "made_of": doc["made_of"]}]
    assert found(run(doc)) == [("LAE-SHACL-001", "warning", "/part/0/identified_by")]


def test_a_node_of_another_record_is_not_ours() -> None:
    """An internal node IRI of a different record is a foreign id like any other."""
    doc = base()
    doc["dimension"][0]["id"] = (
        "https://example.org/object/2/node/0196f3c1-7a2b-7c3d-8e4f-5a6b7c8d9e0f"
    )
    doc["dimension"][0]["value"] = "wide"
    assert found(run(doc)) == [("LAE-SHACL-007", "violation", "/dimension/0/value")]


# --- node index and paths -------------------------------------------------------------------


def test_index_maps_every_subject_to_its_json_pointer() -> None:
    doc = with_node_iris(base())
    index = build_index(doc, doc["id"])
    assert index.path_of(doc["id"]) == ""
    assert index.path_of(doc["identified_by"][1]["id"]) == "/identified_by/1"
    assert index.path_of(doc["produced_by"]["id"]) == "/produced_by"
    assert index.path_of(doc["produced_by"]["timespan"]["id"]) == "/produced_by/timespan"
    assert index.path_of(doc["produced_by"]["part"][0]["id"]) == "/produced_by/part/0"
    assert index.path_of("https://example.org/person/1") is None
    assert index.reference_types["https://example.org/person/1"] == "Person"
    assert index.reference_types["http://vocab.getty.edu/aat/300015050"] == "Material"
    assert len(index.nodes) == 8


def test_index_uses_derived_iris_for_embedded_nodes_with_their_own_id() -> None:
    """ADR-0021: such a node is a subject under ``<record>/node/<uuidv5(id)>``."""
    doc = base()
    doc["identified_by"][0]["id"] = "https://example.org/name/1"
    doc["produced_by"]["id"] = "https://example.org/production/1"
    # The same foreign id again, as a bare pointer to the embedded node.
    doc["referred_to_by"] = [
        {
            "type": "LinguisticObject",
            "content": "About the making",
            "refers_to": [{"id": "https://example.org/production/1", "type": "Production"}],
        }
    ]
    doc = with_node_iris(doc)
    index = build_index(doc, doc["id"])
    name = derived_node_iri(doc["id"], "https://example.org/name/1")
    production = derived_node_iri(doc["id"], "https://example.org/production/1")
    assert index.path_of(name) == "/identified_by/0"
    assert index.path_of(production) == "/produced_by"
    assert "https://example.org/name/1" not in index.nodes
    assert "https://example.org/production/1" not in index.reference_types
    assert index.path_of(doc["produced_by"]["part"][0]["id"]) == "/produced_by/part/0"


def test_the_node_is_the_occurrence_with_content_wherever_it_comes() -> None:
    doc = base()
    doc["referred_to_by"] = [
        {
            "type": "LinguisticObject",
            "content": "About the making",
            "refers_to": [{"id": "https://example.org/production/1", "type": "Production"}],
        }
    ]
    doc["produced_by"]["id"] = "https://example.org/production/1"
    # Put the pointer before the node in document order.
    doc = {key: doc[key] for key in ("@context", "id", "type", "_label", "referred_to_by")} | doc
    assert list(doc).index("referred_to_by") < list(doc).index("produced_by")
    doc = with_node_iris(doc)
    index = build_index(doc, doc["id"])
    production = derived_node_iri(doc["id"], "https://example.org/production/1")
    assert index.path_of(production) == "/produced_by"


def test_a_typed_pointer_to_an_internal_node_is_not_the_node() -> None:
    """``{"id": <node IRI>, "type": ...}`` before the node in document order."""
    doc = with_node_iris(base())
    production = doc["produced_by"]["id"]
    statement = {
        "type": "LinguisticObject",
        "content": "About the making",
        "refers_to": [{"id": production, "type": "Production"}],
    }
    head = {key: doc[key] for key in ("@context", "id", "type", "_label")}
    doc = with_node_iris(head | {"referred_to_by": [statement]} | doc)
    assert list(doc).index("referred_to_by") < list(doc).index("produced_by")
    index = build_index(doc, doc["id"])
    assert index.path_of(production) == "/produced_by"
    doc["produced_by"]["carried_out_by"] = [{"id": "https://example.org/place/1", "type": "Place"}]
    assert found(validate_shacl(doc, doc["id"], {})) == [
        ("LAE-SHACL-005", "violation", "/produced_by/carried_out_by/0")
    ]


def test_a_reference_that_is_not_an_iri_under_an_undefined_property_is_inert() -> None:
    """Expansion drops the property, so the bad id must not reach the data graph either."""
    doc = with_node_iris(base())
    doc["x_custom"] = [
        {"id": "not an <iri>", "type": "Type"},
        {"id": "also\u00a0not", "type": "Type"},
    ]
    assert validate_shacl(doc, doc["id"], {"not an <iri>": "Material"}) == []


def test_paths_through_embedded_nodes_with_their_own_ids() -> None:
    doc = base()
    doc["produced_by"]["id"] = "https://example.org/production/1"
    doc["produced_by"]["part"][0]["id"] = "https://example.org/production/1/part/1"
    doc["produced_by"]["part"][0]["carried_out_by"].append(
        {"id": "https://example.org/place/1", "type": "Place"}
    )
    assert found(run(doc)) == [
        ("LAE-SHACL-005", "violation", "/produced_by/part/0/carried_out_by/1")
    ]


def test_a_value_that_occurs_twice_is_located_by_the_path_that_reaches_it() -> None:
    """The same place is a valid ``took_place_at`` and an invalid ``carried_out_by``."""
    place = {"id": "https://example.org/place/1", "type": "Place"}
    doc = base()
    doc["current_location"] = dict(place)
    doc["produced_by"]["took_place_at"] = [dict(place)]
    doc["produced_by"]["part"].append({"type": "Production", "carried_out_by": [dict(place)]})
    assert found(run(doc)) == [
        ("LAE-SHACL-005", "violation", "/produced_by/part/1/carried_out_by/0")
    ]


def test_nested_parts_are_followed() -> None:
    doc = base()
    doc["produced_by"]["part"][0]["part"] = [
        {
            "type": "Production",
            "carried_out_by": [{"id": "https://example.org/place/1", "type": "Place"}],
        }
    ]
    assert found(run(doc)) == [
        ("LAE-SHACL-005", "violation", "/produced_by/part/0/part/0/carried_out_by/0")
    ]


def test_predicate_keys_follow_the_type_scoped_context() -> None:
    skos_broader = "http://www.w3.org/2004/02/skos/core#broader"
    on_type = shacl._predicate_keys(("Type",), 1)
    on_object = shacl._predicate_keys(("HumanMadeObject",), 1)
    on_activity = shacl._predicate_keys(("Activity",), 1)
    assert on_type[skos_broader] == ("broader", "part_of")
    assert on_object[skos_broader] == ("broader",)
    assert on_object[CRM + "P46i_forms_part_of"] == ("part_of",)
    assert on_activity[CRM + "P9i_forms_part_of"] == ("part_of",)
    assert on_object[CRM + "P45_consists_of"] == ("made_of",)
    assert on_object[CRM + "P190_has_symbolic_content"] == ("content",)
    assert on_object[CRM + "P82a_begin_of_the_begin"] == ("begin_of_the_begin",)
    assert shacl._predicate_keys(("NoSuchClass",), 1) == shacl._predicate_keys((), 1)


# --- failures -------------------------------------------------------------------------------


def test_a_record_that_cannot_be_expanded_gives_an_rdf_violation() -> None:
    doc = base()  # no internal node IRIs: its embedded nodes are blank nodes
    (item,) = validate_shacl(doc, doc["id"], {})
    assert (item.code, item.severity, item.path) == ("LAE-RDF-001", "violation", "")
    assert "blank node" in item.params["detail"]


def test_a_pyshacl_failure_is_a_violation_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise TypeError("'<=' not supported between instances of 'Literal' and 'Literal'")

    monkeypatch.setattr(shacl.pyshacl, "validate", explode)
    (item,) = run(base())
    assert (item.code, item.severity, item.path) == ("LAE-SHACL-000", "violation", "")
    assert "TypeError" in item.params["detail"]


def test_a_result_of_a_shape_without_a_code_is_still_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """README rule 2 broken: a nested property shape is the source shape and has no code."""
    shapes = Graph().parse(
        data=PREFIXES
        + """
        laes:nested a sh:NodeShape ; lae:code "LAE-SHACL-900" ;
            sh:targetClass crm:E22_Human-Made_Object ;
            sh:property [ sh:path crm:P45_consists_of ; sh:maxCount 0 ] .
        """,
        format="turtle",
    )
    monkeypatch.setattr(shacl, "_shapes", lambda: (shapes, shape_codes(shapes)))
    (item,) = run(base())
    assert (item.code, item.severity, item.path) == ("LAE-SHACL-000", "violation", "")
    assert "without lae:code" in item.params["detail"]


def test_shapes_that_cannot_be_loaded_are_a_violation_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> None:
        raise ShapesError("urn:lae:shape:x must have exactly one lae:code")

    monkeypatch.setattr(shacl, "_shapes", broken)
    (item,) = run(base())
    assert (item.code, item.severity, item.path) == ("LAE-SHACL-000", "violation", "")
    assert "ShapesError" in item.params["detail"]


def test_exceeding_the_budget_is_logged_and_validation_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shacl, "SHACL_BUDGET_SECONDS", 0.0)
    doc = base("HumanMadeObject", ["/identified_by", None])
    with structlog.testing.capture_logs() as logs:
        items = run(doc)
    assert found(items) == [("LAE-SHACL-001", "warning", "/identified_by")]
    (event,) = [log for log in logs if log["event"] == "shacl_budget_exceeded"]
    assert event["log_level"] == "warning"
    assert event["record_uri"] == doc["id"]
    assert event["triples"] > 0 and event["seconds"] >= 0


def test_within_the_budget_nothing_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shacl, "SHACL_BUDGET_SECONDS", 60.0)
    with structlog.testing.capture_logs() as logs:
        run(base())
    assert logs == []


def test_lae_code_is_declared_as_an_annotation_property() -> None:
    owl_annotation = "http://www.w3.org/2002/07/owl#AnnotationProperty"
    types = {str(o) for o in shapes_graph().objects(LAE.code, None)}
    assert owl_annotation in types
