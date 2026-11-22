"""Expansion: strict subject ownership, no blank nodes, sanitation, determinism (Step 1.3)."""

from __future__ import annotations

import copy
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pyoxigraph as ox
import pytest
from pyld import jsonld as pyld_jsonld
from pyld.context_resolver import ContextResolver

from lae.ids import derived_node_iri, is_derived_node_iri, is_internal_node_iri
from lae.rdf.canonical import canonical_ntriples, is_canonical_line
from lae.rdf.context import document_loader
from lae.rdf.diff import diff_triples
from lae.rdf.expand import (
    EMBEDDED_TYPES,
    ENTITY_TYPES,
    LA_EQUIVALENT,
    BlankNodeError,
    ExpansionError,
    InvalidIRIError,
    InvalidLiteralError,
    OwnershipError,
    expand_to_ntriples,
    owned_subjects,
    strip_stub_attributes,
)
from lae.sparql.updates import build_delete_where_subjects, build_update

from .node_iris import assign_node_iris, corpus_with_node_iris

CONTEXT = "https://linked.art/ns/v1/linked-art.json"
CRM = "http://www.cidoc-crm.org/cidoc-crm/"
LA = "https://linked.art/ns/terms/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
XSD = "http://www.w3.org/2001/XMLSchema#"

BASE = "https://editor.example.org/id/"
RECORD = BASE + "0192f0c1-0000-7000-8000-000000000001"
OTHER_RECORD = BASE + "0192f0c1-0000-7000-8000-000000000002"
OTHER_RECORD_NODE = OTHER_RECORD + "/node/0192f0c1-0000-7000-8000-0000000000aa"
SIDECAR = "http://vocab.getty.edu/aat/300404670"
SIDECAR_META = "http://vocab.getty.edu/aat/300444970"
UNRESOLVED = "https://unknown.example.com/people/42"
NAME = RECORD + "/node/0192f0c1-0000-7000-8000-0000000000a1"
PRODUCTION = RECORD + "/node/0192f0c1-0000-7000-8000-0000000000a2"
TIMESPAN = RECORD + "/node/0192f0c1-0000-7000-8000-0000000000a3"


def record() -> dict[str, Any]:
    """A record that references a local record, sidecar entities and an unresolved URI."""
    return {
        "@context": CONTEXT,
        "id": RECORD,
        "type": "HumanMadeObject",
        "identified_by": [
            {
                "id": NAME,
                "type": "Name",
                "content": "Night Watch",
                # A sidecar reference that (wrongly, but possibly) still has label, type and a
                # nested description of its own.
                "classified_as": [
                    {
                        "id": SIDECAR,
                        "type": "Type",
                        "_label": "Primary Name",
                        "classified_as": [
                            {"id": SIDECAR_META, "type": "Type", "_label": "Type of Name"}
                        ],
                    }
                ],
            }
        ],
        "produced_by": {
            "id": PRODUCTION,
            "type": "Production",
            "timespan": {
                "id": TIMESPAN,
                "type": "TimeSpan",
                "begin_of_the_begin": "1642-01-01T00:00:00Z",
                "end_of_the_end": "1642-12-31T23:59:59Z",
            },
            # Normalized reference to a local record: id and type only.
            "carried_out_by": [{"id": OTHER_RECORD, "type": "Person"}],
            # Unresolved external URI: kept its _label at write time (ADR-0006).
            "influenced_by": [{"id": UNRESOLVED, "type": "Person", "_label": "Unknown Master"}],
        },
        # Another record's internal node is foreign too.
        "member_of": [{"id": OTHER_RECORD_NODE, "type": "Set", "_label": "A set"}],
    }


def nt(subject: str, predicate: str, obj: str) -> str:
    """One canonical line; ``obj`` is an IRI unless it starts with a quote."""
    return f"<{subject}> <{predicate}> {obj if obj.startswith('"') else f'<{obj}>'} ."


EXPECTED = frozenset(
    {
        nt(RECORD, RDF_TYPE, CRM + "E22_Human-Made_Object"),
        nt(RECORD, CRM + "P1_is_identified_by", NAME),
        nt(RECORD, CRM + "P108i_was_produced_by", PRODUCTION),
        nt(RECORD, LA + "member_of", OTHER_RECORD_NODE),
        nt(NAME, RDF_TYPE, CRM + "E33_E41_Linguistic_Appellation"),
        nt(NAME, CRM + "P190_has_symbolic_content", '"Night Watch"'),
        nt(NAME, CRM + "P2_has_type", SIDECAR),
        nt(PRODUCTION, RDF_TYPE, CRM + "E12_Production"),
        nt(PRODUCTION, CRM + "P4_has_time-span", TIMESPAN),
        nt(PRODUCTION, CRM + "P14_carried_out_by", OTHER_RECORD),
        nt(PRODUCTION, CRM + "P15_was_influenced_by", UNRESOLVED),
        nt(TIMESPAN, RDF_TYPE, CRM + "E52_Time-Span"),
        nt(
            TIMESPAN,
            CRM + "P82a_begin_of_the_begin",
            f'"1642-01-01T00:00:00Z"^^<{XSD}dateTime>',
        ),
        nt(TIMESPAN, CRM + "P82b_end_of_the_end", f'"1642-12-31T23:59:59Z"^^<{XSD}dateTime>'),
    }
)

FOREIGN = (OTHER_RECORD, OTHER_RECORD_NODE, SIDECAR, SIDECAR_META, UNRESOLVED)


def subject_of(line: str) -> str:
    return line[1 : line.index("> ")]


# --------------------------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------------------------


def test_only_the_linking_triple_is_emitted_for_foreign_references() -> None:
    lines = expand_to_ntriples(record(), 1)
    assert lines == EXPECTED
    for foreign in FOREIGN:
        assert not any(subject_of(line) == foreign for line in lines)
    # Exactly one triple mentions each directly referenced foreign entity: the link to it.
    for foreign in (OTHER_RECORD, OTHER_RECORD_NODE, SIDECAR, UNRESOLVED):
        assert sum(f"<{foreign}>" in line for line in lines) == 1
    # Nothing nested inside a foreign reference is emitted at all.
    assert not any(SIDECAR_META in line for line in lines)
    assert not any(RDFS_LABEL in line for line in lines)


def test_every_subject_is_owned() -> None:
    emitted = {subject_of(line) for line in expand_to_ntriples(record(), 1)}
    assert emitted == {RECORD, NAME, PRODUCTION, TIMESPAN}
    # owned_subjects is the superset DELETE WHERE may cover: also the derived node IRI of every
    # foreign id, whether or not this expander version emits anything under it (ADR-0021).
    owned = owned_subjects(record(), RECORD)
    assert owned == emitted | {derived_node_iri(RECORD, foreign) for foreign in FOREIGN}
    assert all(s == RECORD or s.startswith(RECORD + "/node/") for s in owned)


def test_strip_stub_attributes_reduces_foreign_objects_to_id() -> None:
    original = record()
    snapshot = copy.deepcopy(original)
    stripped = strip_stub_attributes(original, RECORD)
    assert original == snapshot, "input must not be modified"
    assert stripped["identified_by"][0]["classified_as"] == [{"id": SIDECAR}]
    assert stripped["produced_by"]["carried_out_by"] == [{"id": OTHER_RECORD}]
    assert stripped["produced_by"]["influenced_by"] == [{"id": UNRESOLVED}]
    assert stripped["member_of"] == [{"id": OTHER_RECORD_NODE}]
    # Owned nodes keep everything else.
    assert stripped["identified_by"][0]["content"] == "Night Watch"
    assert stripped["produced_by"]["timespan"] == original["produced_by"]["timespan"]
    assert stripped["@context"] == CONTEXT
    assert strip_stub_attributes(stripped, RECORD) == stripped


def test_strip_handles_single_valued_and_keyword_aliased_references() -> None:
    doc = {
        "id": RECORD,
        "type": "HumanMadeObject",
        "current_owner": {"id": OTHER_RECORD, "type": "Group", "_label": "Museum"},
        "classified_as": [{"@id": SIDECAR, "@type": "Type", "_label": "x"}],
    }
    stripped = strip_stub_attributes(doc, RECORD)
    assert stripped["current_owner"] == {"id": OTHER_RECORD}
    assert stripped["classified_as"] == [{"id": SIDECAR}]


def test_a_foreign_node_with_owned_looking_children_emits_nothing_below_the_link() -> None:
    inner = RECORD + "/node/0192f0c1-0000-7000-8000-0000000000b1"
    doc = {
        "@context": CONTEXT,
        "id": RECORD,
        "type": "HumanMadeObject",
        "classified_as": [
            {
                "id": SIDECAR,
                "type": "Type",
                "identified_by": [{"id": inner, "type": "Name", "content": "Primary Name"}],
            }
        ],
    }
    assert expand_to_ntriples(doc, 1) == {
        nt(RECORD, RDF_TYPE, CRM + "E22_Human-Made_Object"),
        nt(RECORD, CRM + "P2_has_type", SIDECAR),
    }
    # The node namespace still belongs to the record, so DELETE WHERE may cover it.
    assert owned_subjects(doc, RECORD) == {RECORD, inner, derived_node_iri(RECORD, SIDECAR)}


def test_self_reference_is_owned_and_kept() -> None:
    doc = {
        "@context": CONTEXT,
        "id": RECORD,
        "type": "HumanMadeObject",
        "part_of": [{"id": RECORD, "type": "HumanMadeObject"}],
    }
    assert expand_to_ntriples(doc, 1) == {
        nt(RECORD, RDF_TYPE, CRM + "E22_Human-Made_Object"),
        nt(RECORD, CRM + "P46i_forms_part_of", RECORD),
    }


@pytest.mark.parametrize(
    "lookalike",
    [
        RECORD + "/node/not-a-uuid",
        # A UUIDv5 under /node/ in the JSON-LD is not a derived node IRI: those exist in RDF only.
        RECORD + "/node/c305231a-344b-5671-8231-129262b81e00",
        RECORD + "/node/0192f0c1-0000-7000-8000-0000000000A1",
    ],
)
def test_a_node_iri_that_is_not_a_uuid7_is_foreign(lookalike: str) -> None:
    doc = {
        "@context": CONTEXT,
        "id": RECORD,
        "type": "HumanMadeObject",
        "identified_by": [{"id": lookalike, "type": "Name", "content": "x"}],
        "shows": [{"id": lookalike + "/v", "type": "VisualItem"}],
    }
    derived = derived_node_iri(RECORD, lookalike)
    assert derived != lookalike
    assert expand_to_ntriples(doc, 1) == {
        nt(RECORD, RDF_TYPE, CRM + "E22_Human-Made_Object"),
        nt(RECORD, CRM + "P1_is_identified_by", derived),
        nt(RECORD, CRM + "P65_shows_visual_item", lookalike + "/v"),
        nt(derived, RDF_TYPE, CRM + "E33_E41_Linguistic_Appellation"),
        nt(derived, CRM + "P190_has_symbolic_content", '"x"'),
        nt(derived, LA_EQUIVALENT, lookalike),
    }


def test_root_id_must_be_the_record_uri() -> None:
    with pytest.raises(OwnershipError):
        strip_stub_attributes(record(), OTHER_RECORD)
    with pytest.raises(OwnershipError):
        expand_to_ntriples(record(), 1, record_uri=OTHER_RECORD)


@pytest.mark.parametrize(
    "smuggle",
    [
        # A reverse property makes the foreign entity the subject of the link.
        {
            "id": NAME,
            "type": "Name",
            "@reverse": {f"{CRM}P2_has_type": {"id": SIDECAR}},
        },
        # The same inside an embedded node that is emitted under a derived node IRI.
        {
            "id": "https://museum.example.org/name/1",
            "type": "Name",
            "content": "x",
            "@reverse": {f"{CRM}P2_has_type": {"id": SIDECAR}},
        },
        # A named graph or an included block cannot carry a foreign subject either: the JSON-level
        # strip misses a subject only if its id is spelled in a way it does not know.
        {"id": NAME, "type": "Name", "@reverse": {"classified_as": [{"id": OTHER_RECORD}]}},
    ],
    ids=["reverse", "reverse-in-derived-node", "reverse-term"],
)
def test_foreign_subject_from_any_construct_is_rejected(smuggle: dict[str, Any]) -> None:
    doc = {
        "@context": CONTEXT,
        "id": RECORD,
        "type": "HumanMadeObject",
        "identified_by": [smuggle],
    }
    with pytest.raises(OwnershipError):
        expand_to_ntriples(doc, 1)


@pytest.mark.parametrize(
    "scoped",
    [
        # Renames @id, which would hide a foreign subject from the JSON-level strip.
        {"@context": {"ident": "@id"}, "ident": SIDECAR, "type": "Type", "_label": "smuggled"},
        # Renames a property of an owned node.
        {"@context": {"content": "http://example.org/p"}, "id": NAME, "type": "Name"},
        # Would make an undefined type or a relative id look absolute.
        {"@context": {"@vocab": "http://example.org/"}, "id": NAME, "type": "Nmae"},
        {"@context": {"@base": OTHER_RECORD + "/"}, "id": NAME, "type": "Name"},
        # On an embedded node that has an id of its own.
        {
            "@context": {"content": "http://example.org/p"},
            "id": "https://museum.example.org/name/1",
            "type": "Name",
            "content": "x",
        },
    ],
)
def test_a_context_below_the_root_is_rejected_on_any_node_of_the_record(
    scoped: dict[str, Any],
) -> None:
    doc = {"@context": CONTEXT, "id": RECORD, "type": "HumanMadeObject", "identified_by": [scoped]}
    with pytest.raises(ExpansionError, match="@context"):
        expand_to_ntriples(doc, 1)
    with pytest.raises(ExpansionError, match="@context"):
        strip_stub_attributes(doc, RECORD)


def test_a_context_inside_a_reference_is_dropped_with_the_rest() -> None:
    doc = {
        "@context": CONTEXT,
        "id": RECORD,
        "type": "HumanMadeObject",
        "classified_as": [
            {"id": SIDECAR, "type": "Type", "@context": {"x": "http://example.org/x"}, "x": "y"}
        ],
        "about": [{"id": OTHER_RECORD, "type": "Activity", "@context": "https://example.org/c"}],
    }
    assert expand_to_ntriples(doc, 1) == {
        nt(RECORD, RDF_TYPE, CRM + "E22_Human-Made_Object"),
        nt(RECORD, CRM + "P2_has_type", SIDECAR),
        nt(RECORD, CRM + "P129_is_about", OTHER_RECORD),
    }


# --------------------------------------------------------------------------------------------
# Embedded nodes that arrive with an id of their own (ADR-0021)
# --------------------------------------------------------------------------------------------

MUSEUM = "https://museum.example.org/"
M_NAME = MUSEUM + "name/1"
M_IDENTIFIER = MUSEUM + "identifier/1"
M_PRODUCTION = MUSEUM + "production/1"
M_TIMESPAN = MUSEUM + "timespan/1642"
M_DIMENSION = MUSEUM + "dimension/1"
M_STATEMENT = MUSEUM + "statement/1"
M_ASSIGNMENT = MUSEUM + "assignment/1"
PRIMARY = "http://vocab.getty.edu/aat/300404670"
META = "http://vocab.getty.edu/aat/300444970"
ULAN = "http://vocab.getty.edu/ulan/500011051"
CM = "http://vocab.getty.edu/aat/300379098"
OTHER_RECORD_2 = BASE + "0192f0c1-0000-7000-8000-000000000003"


def institutional(record_uri: str = RECORD, *, title: str = "Night Watch") -> dict[str, Any]:
    """A record as an institution exports it: every embedded node has an IRI the source minted."""
    return {
        "@context": CONTEXT,
        "id": record_uri,
        "type": "HumanMadeObject",
        "identified_by": [
            {
                "id": M_NAME,
                "type": "Name",
                "content": title,
                # A concept reference with its meta-type inline: still only the link.
                "classified_as": [
                    {
                        "id": PRIMARY,
                        "type": "Type",
                        "_label": "Primary Name",
                        "classified_as": [{"id": META, "type": "Type"}],
                    }
                ],
            },
            {"id": M_IDENTIFIER, "type": "Identifier", "content": "SK-C-5"},
        ],
        "produced_by": {
            "id": M_PRODUCTION,
            "type": "Production",
            "timespan": {
                "id": M_TIMESPAN,
                "type": "TimeSpan",
                "begin_of_the_begin": "1642-01-01T00:00:00Z",
            },
            # An entity reference that says something about its target: still only the link.
            "carried_out_by": [
                {
                    "id": ULAN,
                    "type": "Person",
                    "_label": "Rembrandt",
                    "identified_by": [{"id": MUSEUM + "name/2", "type": "Name", "content": "R."}],
                }
            ],
        },
        "dimension": [{"id": M_DIMENSION, "type": "Dimension", "value": 363, "unit": {"id": CM}}],
        "referred_to_by": [
            {"id": M_STATEMENT, "type": "LinguisticObject", "content": "Oil on canvas"}
        ],
        # The assignment is embedded; what it assigned is the Name above, by id only.
        "attributed_by": [
            {
                "id": M_ASSIGNMENT,
                "type": "AttributeAssignment",
                "assigned": [{"id": M_NAME, "type": "Name"}],
            }
        ],
    }


def institutional_expected(record_uri: str = RECORD, *, title: str = "Night Watch") -> set[str]:
    def d(original: str) -> str:
        return derived_node_iri(record_uri, original)

    return {
        nt(record_uri, RDF_TYPE, CRM + "E22_Human-Made_Object"),
        nt(record_uri, CRM + "P1_is_identified_by", d(M_NAME)),
        nt(record_uri, CRM + "P1_is_identified_by", d(M_IDENTIFIER)),
        nt(record_uri, CRM + "P108i_was_produced_by", d(M_PRODUCTION)),
        nt(record_uri, CRM + "P43_has_dimension", d(M_DIMENSION)),
        nt(record_uri, CRM + "P67i_is_referred_to_by", d(M_STATEMENT)),
        nt(record_uri, CRM + "P140i_was_attributed_by", d(M_ASSIGNMENT)),
        nt(d(M_NAME), RDF_TYPE, CRM + "E33_E41_Linguistic_Appellation"),
        nt(d(M_NAME), CRM + "P190_has_symbolic_content", f'"{title}"'),
        nt(d(M_NAME), CRM + "P2_has_type", PRIMARY),
        nt(d(M_NAME), LA_EQUIVALENT, M_NAME),
        nt(d(M_IDENTIFIER), RDF_TYPE, CRM + "E42_Identifier"),
        nt(d(M_IDENTIFIER), CRM + "P190_has_symbolic_content", '"SK-C-5"'),
        nt(d(M_IDENTIFIER), LA_EQUIVALENT, M_IDENTIFIER),
        nt(d(M_PRODUCTION), RDF_TYPE, CRM + "E12_Production"),
        nt(d(M_PRODUCTION), CRM + "P4_has_time-span", d(M_TIMESPAN)),
        nt(d(M_PRODUCTION), CRM + "P14_carried_out_by", ULAN),
        nt(d(M_PRODUCTION), LA_EQUIVALENT, M_PRODUCTION),
        nt(d(M_TIMESPAN), RDF_TYPE, CRM + "E52_Time-Span"),
        nt(
            d(M_TIMESPAN),
            CRM + "P82a_begin_of_the_begin",
            f'"1642-01-01T00:00:00Z"^^<{XSD}dateTime>',
        ),
        nt(d(M_TIMESPAN), LA_EQUIVALENT, M_TIMESPAN),
        nt(d(M_DIMENSION), RDF_TYPE, CRM + "E54_Dimension"),
        nt(d(M_DIMENSION), CRM + "P90_has_value", f'"363"^^<{XSD}integer>'),
        nt(d(M_DIMENSION), CRM + "P91_has_unit", CM),
        nt(d(M_DIMENSION), LA_EQUIVALENT, M_DIMENSION),
        nt(d(M_STATEMENT), RDF_TYPE, CRM + "E33_Linguistic_Object"),
        nt(d(M_STATEMENT), CRM + "P190_has_symbolic_content", '"Oil on canvas"'),
        nt(d(M_STATEMENT), LA_EQUIVALENT, M_STATEMENT),
        nt(d(M_ASSIGNMENT), RDF_TYPE, CRM + "E13_Attribute_Assignment"),
        nt(d(M_ASSIGNMENT), CRM + "P141_assigned", d(M_NAME)),
        nt(d(M_ASSIGNMENT), LA_EQUIVALENT, M_ASSIGNMENT),
    }


def test_embedded_nodes_with_their_own_ids_are_indexed_under_derived_node_iris() -> None:
    doc = institutional()
    snapshot = copy.deepcopy(doc)
    lines = expand_to_ntriples(doc, 1)
    assert doc == snapshot, "the JSON-LD keeps the original ids; only the RDF is rewritten"
    assert lines == institutional_expected()
    subjects = {subject_of(line) for line in lines}
    assert all(s == RECORD or is_derived_node_iri(s, RECORD) for s in subjects)
    assert not any(is_internal_node_iri(s, RECORD) for s in subjects)
    assert subjects <= owned_subjects(doc, RECORD)
    # Nothing the institution minted, and nothing inside a reference, is a subject or leaks.
    assert not any(subject_of(line).startswith(MUSEUM) for line in lines)
    assert not any(META in line or "name/2" in line or RDFS_LABEL in line for line in lines)


def test_two_records_embedding_a_node_with_the_same_foreign_id_share_no_subject() -> None:
    a = expand_to_ntriples(institutional(RECORD, title="Night Watch"), 1)
    b = expand_to_ntriples(institutional(OTHER_RECORD_2, title="De Nachtwacht"), 1)
    assert a == institutional_expected(RECORD, title="Night Watch")
    assert b == institutional_expected(OTHER_RECORD_2, title="De Nachtwacht")
    assert not {subject_of(line) for line in a} & {subject_of(line) for line in b}
    assert not a & b
    owned_a = owned_subjects(institutional(RECORD), RECORD)
    owned_b = owned_subjects(institutional(OTHER_RECORD_2), OTHER_RECORD_2)
    assert not owned_a & owned_b
    # Both records' content is findable through the id the institution minted.
    for lines, uri, title in ((a, RECORD, "Night Watch"), (b, OTHER_RECORD_2, "De Nachtwacht")):
        node = derived_node_iri(uri, M_NAME)
        assert nt(node, LA_EQUIVALENT, M_NAME) in lines
        assert nt(node, CRM + "P190_has_symbolic_content", f'"{title}"') in lines

    def triples(store: ox.Store) -> set[str]:
        return {f"{q.subject} {q.predicate} {q.object} ." for q in store}

    # Editing A's copy of the shared-id Name is one delete and one insert, and leaves B alone.
    store = ox.Store()
    store.update(build_update([], a | b))
    assert triples(store) == a | b
    a2 = expand_to_ntriples(institutional(RECORD, title="The Night Watch"), 1)
    to_delete, to_insert = diff_triples(a, a2)
    assert (len(to_delete), len(to_insert)) == (1, 1)
    store.update(build_update(to_delete, to_insert))
    assert triples(store) == a2 | b
    # Removing A altogether, by diff or by DELETE WHERE over its owned subjects, leaves B whole.
    by_diff = ox.Store()
    by_diff.update(build_update([], a2 | b))
    by_diff.update(build_update(a2, []))
    assert triples(by_diff) == b
    store.update(build_delete_where_subjects(owned_a))
    assert triples(store) == b


@pytest.mark.parametrize(
    ("obj", "embedded"),
    [
        # Stubs are references, whatever their class.
        ({"type": "Name"}, False),
        ({"type": "Activity", "_label": "x"}, False),
        ({}, False),
        # Classes Linked Art never embeds are references, whatever they carry.
        ({"type": "Type", "classified_as": [{"id": META}]}, False),
        ({"type": "Language", "notation": "en"}, False),
        ({"type": "Currency", "notation": "USD", "identified_by": [{"type": "Name"}]}, False),
        ({"type": "Person", "identified_by": [{"type": "Name", "content": "x"}]}, False),
        ({"type": "Group", "classified_as": [{"id": META}], "residence": []}, False),
        ({"type": "Place", "defined_by": "POINT(1 1)"}, False),
        ({"type": "HumanMadeObject", "made_of": [{"id": META}]}, False),
        ({"type": "Set", "member": []}, False),
        ({"@type": "Type", "equivalent": [{"id": META}]}, False),
        # Classes that exist only inside a record are embedded as soon as they say anything.
        ({"type": "Name", "content": "x"}, True),
        ({"type": "Name", "classified_as": [{"id": META}]}, True),
        ({"type": "Identifier", "content": "x"}, True),
        ({"type": "TimeSpan", "begin_of_the_begin": "1642-01-01T00:00:00Z"}, True),
        ({"type": "Dimension", "value": 1}, True),
        ({"type": "Production", "carried_out_by": [{"id": ULAN}]}, True),
        ({"@type": "MonetaryAmount", "value": 1}, True),
        # Any other class: a reference may repeat its target's classification, nothing more.
        ({"type": "Activity", "classified_as": [{"id": META}]}, False),
        ({"type": "Activity", "classified_as": [{"id": META}], "timespan": {}}, True),
        ({"type": "LinguisticObject", "_label": "x", "classified_as": [{"id": META}]}, False),
        ({"type": "LinguisticObject", "content": "x"}, True),
        ({"type": "DigitalObject", "access_point": [{"id": META}]}, True),
        ({"type": "VisualItem", "represents": [{"id": ULAN}]}, True),
        ({"type": "NotAClass", "content": "x"}, True),
        ({"content": "x"}, True),
        ({"type": ["LinguisticObject", "Type"], "content": "x"}, False),
    ],
)
def test_reference_or_embedded_node(obj: dict[str, Any], embedded: bool) -> None:
    foreign = MUSEUM + "thing/1"
    doc = {"id": RECORD, "type": "HumanMadeObject", "referred_to_by": [{"id": foreign, **obj}]}
    stripped = strip_stub_attributes(doc, RECORD)["referred_to_by"][0]
    if embedded:
        assert stripped == {**obj, "id": derived_node_iri(RECORD, foreign)} or (
            stripped["id"] == derived_node_iri(RECORD, foreign) and stripped.keys() == {"id", *obj}
        )
    else:
        assert stripped == {"id": foreign}


def test_content_inside_an_embedded_node_follows_the_same_rule() -> None:
    inner_owned = RECORD + "/node/0192f0c1-0000-7000-8000-0000000000c1"
    doc = {
        "@context": CONTEXT,
        "id": RECORD,
        "type": "HumanMadeObject",
        "produced_by": {
            "id": M_PRODUCTION,
            "type": "Production",
            "part": [
                {
                    "id": inner_owned,
                    "type": "Production",
                    "timespan": {"id": M_TIMESPAN, "type": "TimeSpan", "_label": "1642"},
                    "technique": [{"id": PRIMARY, "type": "Type", "_label": "x"}],
                }
            ],
        },
    }
    d = derived_node_iri(RECORD, M_PRODUCTION)
    assert expand_to_ntriples(doc, 1) == {
        nt(RECORD, RDF_TYPE, CRM + "E22_Human-Made_Object"),
        nt(RECORD, CRM + "P108i_was_produced_by", d),
        nt(d, RDF_TYPE, CRM + "E12_Production"),
        nt(d, CRM + "P9_consists_of", inner_owned),
        nt(d, LA_EQUIVALENT, M_PRODUCTION),
        nt(inner_owned, RDF_TYPE, CRM + "E12_Production"),
        # A stub is a reference even when its class is embedded-only: the link, nothing else.
        nt(inner_owned, CRM + "P4_has_time-span", M_TIMESPAN),
        nt(inner_owned, CRM + "P32_used_general_technique", PRIMARY),
    }


def test_the_same_foreign_id_embedded_twice_is_one_node() -> None:
    doc = {
        "@context": CONTEXT,
        "id": RECORD,
        "type": "HumanMadeObject",
        "identified_by": [
            {"id": M_NAME, "type": "Name", "content": "a"},
            {"id": M_NAME, "type": "Name", "content": "b"},
        ],
    }
    d = derived_node_iri(RECORD, M_NAME)
    assert expand_to_ntriples(doc, 1) == {
        nt(RECORD, RDF_TYPE, CRM + "E22_Human-Made_Object"),
        nt(RECORD, CRM + "P1_is_identified_by", d),
        nt(d, RDF_TYPE, CRM + "E33_E41_Linguistic_Appellation"),
        nt(d, CRM + "P190_has_symbolic_content", '"a"'),
        nt(d, CRM + "P190_has_symbolic_content", '"b"'),
        nt(d, LA_EQUIVALENT, M_NAME),
    }


@pytest.mark.parametrize(
    ("bad", "error"),
    [
        ("name/1", InvalidIRIError),
        ("https://museum.example.org/name 1", InvalidIRIError),
        ("https://museum.example.org/name\u00a01", InvalidIRIError),
        ('https://museum.example.org/name"1', InvalidIRIError),
        ("_:b0", BlankNodeError),
    ],
)
def test_an_embedded_node_with_an_unwritable_id_is_rejected(
    bad: str, error: type[Exception]
) -> None:
    doc = {
        "@context": CONTEXT,
        "id": RECORD,
        "type": "HumanMadeObject",
        "identified_by": [{"id": bad, "type": "Name", "content": "x"}],
    }
    with pytest.raises(error):
        expand_to_ntriples(doc, 1)


# --------------------------------------------------------------------------------------------
# Blank nodes
# --------------------------------------------------------------------------------------------


def test_node_without_internal_iri_raises() -> None:
    doc = record()
    del doc["produced_by"]["timespan"]["id"]
    with pytest.raises(BlankNodeError):
        expand_to_ntriples(doc, 1)


def test_record_without_root_id_raises() -> None:
    doc = record()
    del doc["id"]
    with pytest.raises(BlankNodeError):
        expand_to_ntriples(doc, 1)


def test_explicit_blank_node_identifier_raises() -> None:
    doc = record()
    doc["produced_by"]["timespan"]["id"] = "_:b0"
    with pytest.raises(BlankNodeError):
        expand_to_ntriples(doc, 1)


def test_a_literal_containing_blank_node_syntax_is_fine() -> None:
    doc = record()
    doc["identified_by"][0]["content"] = "_:b0 <x> _:b1 ."
    assert nt(NAME, CRM + "P190_has_symbolic_content", '"_:b0 <x> _:b1 ."') in expand_to_ntriples(
        doc, 1
    )


# --------------------------------------------------------------------------------------------
# Sanitation
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bound",
    [
        "-0500-01-01T00:00:00Z",
        "-0044-03-15T00:00:00Z",
        "-10000-01-01T00:00:00Z",
        "0001-01-01T00:00:00Z",
        "1642-01-01T00:00:00",
        "1642-01-01T00:00:00.5+01:00",
    ],
)
def test_valid_datetimes_including_bce_survive(bound: str) -> None:
    doc = record()
    doc["produced_by"]["timespan"]["begin_of_the_begin"] = bound
    line = nt(TIMESPAN, CRM + "P82a_begin_of_the_begin", f'"{bound}"^^<{XSD}dateTime>')
    assert line in expand_to_ntriples(doc, 1)


@pytest.mark.parametrize(
    "bound",
    ["1642", "1642-01-01", "500 BCE", "", " 1642-01-01T00:00:00Z", "1642-01-01T00:00:00Z\n", 1642],
)
def test_invalid_datetime_is_rejected_not_emitted(bound: Any) -> None:
    doc = record()
    doc["produced_by"]["timespan"]["begin_of_the_begin"] = bound
    with pytest.raises(InvalidLiteralError):
        expand_to_ntriples(doc, 1)


@pytest.mark.parametrize(
    "bad",
    [
        "https://example.org/a b",
        "https://example.org/a>b",
        'https://example.org/a"b',
        "https://example.org/a\\b",
        "https://example.org/{a}",
        "https://example.org/a\nb",
        "https:/",
        "relative/path",
        "",
        "#fragment",
    ],
)
def test_invalid_reference_iri_is_rejected_not_dropped(bad: str) -> None:
    """pyld alone would silently drop the triple or resolve the IRI against a default base."""
    doc = record()
    doc["produced_by"]["carried_out_by"] = [{"id": bad, "type": "Person"}]
    with pytest.raises(InvalidIRIError):
        expand_to_ntriples(doc, 1)


@pytest.mark.parametrize(
    "space", ["\u00a0", "\u0085", "\u1680", "\u2003", "\u2028", "\u2029", "\u202f", "\u3000"]
)
def test_an_iri_with_unicode_whitespace_is_rejected_not_dropped(space: str) -> None:
    """N-Triples allows these in an IRI, but pyld drops the triple; so reject."""
    bad = f"https://unknown.example.com/a{space}b"
    doc = record()
    doc["produced_by"]["carried_out_by"] = [{"id": bad, "type": "Person"}]
    with pytest.raises(InvalidIRIError):
        expand_to_ntriples(doc, 1)
    doc = record()
    doc["id"] = RECORD + space
    with pytest.raises(InvalidIRIError):
        expand_to_ntriples(doc, 1)


def test_invalid_iri_given_as_a_bare_string_is_rejected() -> None:
    doc = record()
    doc["produced_by"]["carried_out_by"] = ["https://example.org/a b"]
    with pytest.raises(InvalidIRIError):
        expand_to_ntriples(doc, 1)


@pytest.mark.parametrize("bad_type", ["NoSuchClass", "https://example.org/a b"])
def test_undefined_or_malformed_type_is_rejected(bad_type: str) -> None:
    doc = record()
    doc["identified_by"][0]["type"] = bad_type
    with pytest.raises(InvalidIRIError):
        expand_to_ntriples(doc, 1)


def test_invalid_record_uri_is_rejected() -> None:
    doc = {"@context": CONTEXT, "id": "https://example.org/a b", "type": "HumanMadeObject"}
    with pytest.raises(InvalidIRIError):
        expand_to_ntriples(doc, 1)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_number_is_rejected(value: float) -> None:
    doc = record()
    doc["dimension"] = [
        {"id": RECORD + "/node/0192f0c1-0000-7000-8000-0000000000c1", "type": "Dimension"}
    ]
    doc["dimension"][0]["value"] = value
    with pytest.raises(InvalidLiteralError):
        expand_to_ntriples(doc, 1)


def test_numbers_and_booleans_have_canonical_lexical_forms() -> None:
    node = RECORD + "/node/0192f0c1-0000-7000-8000-0000000000c1"
    doc = {
        "@context": CONTEXT,
        "id": RECORD,
        "type": "HumanMadeObject",
        "dimension": [{"id": node, "type": "Dimension", "value": 0}],
    }
    # A float with no fractional part is an integer (JSON-LD), so 10 and 10.0 are one literal and
    # a store that turns one into the other (a CRDT double) does not change the triples.
    expected: list[tuple[Any, str]] = [
        (10, f'"10"^^<{XSD}integer>'),
        (-3, f'"-3"^^<{XSD}integer>'),
        (2**63, f'"{2**63}"^^<{XSD}integer>'),
        # JSON-LD turns integers of 1e21 and above into doubles.
        (10**21, f'"1.0E21"^^<{XSD}double>'),
        (10.5, f'"1.05E1"^^<{XSD}double>'),
        (10.0, f'"10"^^<{XSD}integer>'),
        (1e-7, f'"1.0E-7"^^<{XSD}double>'),
        (True, f'"true"^^<{XSD}boolean>'),
    ]
    for value, literal in expected:
        doc["dimension"][0]["value"] = value
        assert nt(node, CRM + "P90_has_value", literal) in expand_to_ntriples(doc, 1)


def test_lone_surrogate_is_rejected() -> None:
    doc = record()
    doc["identified_by"][0]["content"] = "bad \ud800 text"
    with pytest.raises(InvalidLiteralError):
        expand_to_ntriples(doc, 1)


NASTY = (
    'quote " backslash \\ tab \t newline \n return \r nul \x00 bell \x07 del \x7f '
    "é 日本 \U0001f600 } ; <x>"
    # Text that looks like a SPARQL codepoint escape (see lae.rdf.canonical).
    " \\u0041 \\u0022 \\U0001F600 \\\\u005C "
)


def test_literal_escaping_round_trips_through_a_real_parser() -> None:
    doc = record()
    doc["identified_by"][0]["content"] = NASTY
    lines = expand_to_ntriples(doc, 1)
    assert all(is_canonical_line(line) for line in lines)
    assert all("\n" not in line and "\r" not in line for line in lines)
    parsed = list(ox.parse(canonical_ntriples(lines), format=ox.RdfFormat.N_TRIPLES))
    assert len(parsed) == len(lines)
    contents = [
        t.object.value for t in parsed if t.predicate.value == CRM + "P190_has_symbolic_content"
    ]
    assert contents == [NASTY]


def test_context_must_be_the_pinned_one() -> None:
    doc = record()
    doc["@context"] = "https://example.org/context.json"
    with pytest.raises(ExpansionError):
        expand_to_ntriples(doc, 1)
    doc["@context"] = [CONTEXT, {"content": "http://example.org/other"}]
    with pytest.raises(ExpansionError):
        expand_to_ntriples(doc, 1)
    with pytest.raises(ExpansionError):
        expand_to_ntriples(record(), 999)


def test_missing_context_gets_the_pinned_one() -> None:
    doc = record()
    del doc["@context"]
    assert expand_to_ntriples(doc, 1) == EXPECTED


def test_unpinned_nested_context_is_refused_without_network() -> None:
    doc = record()
    doc["identified_by"][0]["@context"] = "https://example.org/context.json"
    with pytest.raises(ExpansionError):
        expand_to_ntriples(doc, 1)


def test_duplicate_statements_collapse() -> None:
    doc = record()
    doc["produced_by"]["carried_out_by"] *= 2
    assert expand_to_ntriples(doc, 1) == EXPECTED


# --------------------------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------------------------


def _pyld_reference_triples(doc: dict[str, Any]) -> set[ox.Triple]:
    """The record's triples by pyld's own N-Quads writer, with nothing stripped."""
    options = {
        "documentLoader": document_loader,
        "contextResolver": ContextResolver({}, document_loader),
        "format": "application/n-quads",
    }
    nquads = pyld_jsonld.to_rdf(doc, options)
    return {
        ox.Triple(q.subject, q.predicate, q.object)
        for q in ox.parse(nquads, format=ox.RdfFormat.N_QUADS)
    }


def _embedded_foreign_ids(doc: dict[str, Any], record_uri: str) -> set[str]:
    """ADR-0021's rule, written again without the expander's helpers."""
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            ident = value.get("id")
            if isinstance(ident, str) and not (
                ident == record_uri or ident.startswith(record_uri + "/node/")
            ):
                said = set(value) - {"id", "type", "_label", "@context"}
                kind = value.get("type")
                if not said or kind in ENTITY_TYPES:
                    return
                if kind not in EMBEDDED_TYPES and said == {"classified_as"}:
                    return
                found.add(ident)
            for key, item in value.items():
                if key != "@context":
                    visit(item)

    visit(doc)
    return found


def _reachable_owned_triples(
    triples: set[ox.Triple], record_uri: str, embedded: set[str]
) -> set[ox.Triple]:
    """Independent statement of the ownership rule, on the unstripped graph.

    Rename each embedded node that has a foreign id to its derived node IRI and link it back to
    that id. Then keep the triples of every owned subject that can be reached from the record by
    following links through owned subjects only.
    """
    renamed = {iri: ox.NamedNode(derived_node_iri(record_uri, iri)) for iri in embedded}

    def rename(term: Any) -> Any:
        return renamed.get(term.value, term) if isinstance(term, ox.NamedNode) else term

    triples = {ox.Triple(rename(t.subject), t.predicate, rename(t.object)) for t in triples}
    triples |= {
        ox.Triple(node, ox.NamedNode(LA_EQUIVALENT), ox.NamedNode(iri))
        for iri, node in renamed.items()
    }
    by_subject: dict[str, list[ox.Triple]] = {}
    for triple in triples:
        assert isinstance(triple.subject, ox.NamedNode)
        by_subject.setdefault(triple.subject.value, []).append(triple)
    owned = owned_subjects_by_prefix(by_subject, record_uri)
    kept: set[ox.Triple] = set()
    seen, queue = {record_uri}, [record_uri]
    while queue:
        for triple in by_subject.get(queue.pop(), []):
            kept.add(triple)
            target = triple.object
            if isinstance(target, ox.NamedNode) and target.value in owned:
                if target.value not in seen:
                    seen.add(target.value)
                    queue.append(target.value)
    return kept


def owned_subjects_by_prefix(by_subject: dict[str, Any], record_uri: str) -> set[str]:
    return {s for s in by_subject if s == record_uri or s.startswith(record_uri + "/node/")}


CORPUS = list(corpus_with_node_iris())


def test_corpus_is_present() -> None:
    assert len(CORPUS) >= 244


@pytest.mark.parametrize(("name", "doc"), CORPUS, ids=[name for name, _ in CORPUS])
def test_corpus_record_expands_to_owned_canonical_triples(name: str, doc: dict[str, Any]) -> None:
    record_uri = doc["id"]
    lines = expand_to_ntriples(doc, 1)
    assert lines, "every corpus record has at least its rdf:type"
    owned = owned_subjects(doc, record_uri)
    for line in lines:
        assert is_canonical_line(line)
        assert "_:" not in line.split(" ", 1)[0]
        assert subject_of(line) in owned

    # The same graph as pyld's own serializer gives, restricted by the ownership rule.
    ours = {
        ox.Triple(q.subject, q.predicate, q.object)
        for q in ox.parse(canonical_ntriples(lines), format=ox.RdfFormat.N_TRIPLES)
    }
    assert len(ours) == len(lines)
    embedded = _embedded_foreign_ids(doc, record_uri)
    assert ours == _reachable_owned_triples(_pyld_reference_triples(doc), record_uri, embedded)
    for iri in embedded:
        assert nt(derived_node_iri(record_uri, iri), LA_EQUIVALENT, iri) in lines


def test_corpus_embedded_nodes_with_their_own_ids_are_indexed() -> None:
    """The three corpus Names that have an id of their own, and nothing else, become derived."""
    found = {(name, iri) for name, doc in CORPUS for iri in _embedded_foreign_ids(doc, doc["id"])}
    assert found == {
        ("object-3.json", "https://linked.art/example/Name/0"),
        ("object-nightwatch-43.json", "https://linked.art/example/Name/1642"),
        ("person-rembrandt-4.json", "https://linked.art/example/Name/Harmenzoon"),
    }
    doc = dict(CORPUS)["object-3.json"]
    node = derived_node_iri(doc["id"], "https://linked.art/example/Name/0")
    contents = [
        line
        for line in expand_to_ntriples(doc, 1)
        if line.startswith(f"<{node}> <{CRM}P190_has_symbolic_content> ")
    ]
    assert len(contents) == 1


def test_corpus_has_no_blank_nodes_only_once_node_iris_are_assigned() -> None:
    """The helper matters: without node IRIs, records with embedded nodes are rejected."""
    from lae.corpus import iter_corpus

    rejected = 0
    for _, raw in iter_corpus():
        try:
            expand_to_ntriples(raw, 1)
        except BlankNodeError:
            rejected += 1
    assert rejected > 100


# --------------------------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------------------------


def corpus_digest() -> str:
    """SHA-256 over the canonical N-Triples of every corpus record, in corpus order."""
    digest = hashlib.sha256()
    for name, doc in corpus_with_node_iris():
        digest.update(name.encode())
        digest.update(canonical_ntriples(expand_to_ntriples(doc, 1)))
    return digest.hexdigest()


def test_expansion_is_deterministic_within_a_process() -> None:
    assert corpus_digest() == corpus_digest()
    for _, doc in CORPUS[:20]:
        shuffled = dict(reversed(list(doc.items())))
        assert expand_to_ntriples(shuffled, 1) == expand_to_ntriples(doc, 1)


def test_expansion_is_deterministic_across_processes() -> None:
    """Two fresh interpreters with different hash seeds produce byte-identical N-Triples."""
    backend = Path(__file__).resolve().parents[2]
    script = "from tests.rdf.test_expand import corpus_digest; print(corpus_digest())"
    digests = []
    for seed in ("1", "2718"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=backend,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        digests.append(result.stdout.strip())
    assert digests[0] == digests[1] == corpus_digest()
    assert len(digests[0]) == 64


# --------------------------------------------------------------------------------------------
# Diff of one edit
# --------------------------------------------------------------------------------------------


def test_one_changed_title_is_one_delete_and_one_insert() -> None:
    old = record()
    new = record()
    new["identified_by"][0]["content"] = "The Night Watch"
    to_delete, to_insert = diff_triples(expand_to_ntriples(old, 1), expand_to_ntriples(new, 1))
    assert to_delete == {nt(NAME, CRM + "P190_has_symbolic_content", '"Night Watch"')}
    assert to_insert == {nt(NAME, CRM + "P190_has_symbolic_content", '"The Night Watch"')}


def test_one_changed_title_in_a_corpus_record() -> None:
    name, doc = next(
        (name, doc)
        for name, doc in CORPUS
        if any("content" in n for n in doc.get("identified_by", []))
    )
    edited = copy.deepcopy(doc)
    title = next(n for n in edited["identified_by"] if "content" in n)
    title["content"] += " (edited)"
    to_delete, to_insert = diff_triples(expand_to_ntriples(doc, 1), expand_to_ntriples(edited, 1))
    assert len(to_delete) == 1 and len(to_insert) == 1, name
    assert subject_of(next(iter(to_delete))) == subject_of(next(iter(to_insert))) == title["id"]


def test_relabelling_a_foreign_reference_changes_nothing() -> None:
    """A renamed artist must not churn the records that point at it (spec §3.2)."""
    new = record()
    new["produced_by"]["influenced_by"][0]["_label"] = "Renamed Master"
    new["identified_by"][0]["classified_as"][0]["_label"] = "Preferred Name"
    assert diff_triples(expand_to_ntriples(record(), 1), expand_to_ntriples(new, 1)) == (
        frozenset(),
        frozenset(),
    )


def test_assign_node_iris_helper_mints_owned_iris() -> None:
    doc = assign_node_iris(
        {"id": RECORD, "type": "HumanMadeObject", "identified_by": [{"type": "Name"}]}, RECORD
    )
    assert owned_subjects(doc, RECORD) == {RECORD, doc["identified_by"][0]["id"]}
