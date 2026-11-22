"""Public Turtle serialization."""

from __future__ import annotations

import pyoxigraph as ox
import pytest

from lae.corpus import iter_corpus
from lae.rdf.canonical import canonical_ntriples
from lae.rdf.expand import InvalidIRIError, expand_to_ntriples
from lae.rdf.serialize import context_prefixes, to_turtle

from .node_iris import assign_node_iris

CONTEXT = "https://linked.art/ns/v1/linked-art.json"
CRM = "http://www.cidoc-crm.org/cidoc-crm/"
RECORD = "https://editor.example.org/id/0192f0c1-0000-7000-8000-000000000001"

PUBLIC = {
    "@context": CONTEXT,
    "id": RECORD,
    "type": "HumanMadeObject",
    "_label": "Night Watch",
    "identified_by": [{"type": "Name", "content": 'The "Night Watch"'}],
    "classified_as": [
        {"id": "http://vocab.getty.edu/aat/300033618", "type": "Type", "_label": "Painting"}
    ],
}


def parse(turtle: str) -> set[ox.Triple]:
    return {
        ox.Triple(q.subject, q.predicate, q.object)
        for q in ox.parse(turtle, format=ox.RdfFormat.TURTLE)
    }


def test_turtle_of_a_public_record_uses_prefixes_and_blank_nodes() -> None:
    turtle = to_turtle(PUBLIC, 1)
    assert "@prefix crm: <http://www.cidoc-crm.org/cidoc-crm/> ." in turtle
    assert "crm:E22_Human-Made_Object" in turtle
    triples = parse(turtle)
    assert len(triples) == 8
    blank = {t.subject for t in triples if isinstance(t.subject, ox.BlankNode)}
    assert len(blank) == 1
    assert (
        ox.Triple(ox.NamedNode(RECORD), ox.NamedNode(CRM + "P1_is_identified_by"), blank.pop())
        in triples
    )
    # Public output is not ownership-stripped: transcluded labels and types are included.
    assert (
        ox.Triple(
            ox.NamedNode("http://vocab.getty.edu/aat/300033618"),
            ox.NamedNode("http://www.w3.org/2000/01/rdf-schema#label"),
            ox.Literal("Painting"),
        )
        in triples
    )
    assert any(t.object == ox.Literal('The "Night Watch"') for t in triples)


def test_turtle_is_deterministic() -> None:
    assert to_turtle(PUBLIC, 1) == to_turtle(PUBLIC, 1)


def test_turtle_with_node_iris_matches_the_unstripped_expansion() -> None:
    """With node IRIs kept, Turtle holds a superset of the synced triples."""
    for _, record in list(iter_corpus())[:25]:
        doc = assign_node_iris(record, record["id"])
        turtle_triples = parse(to_turtle(doc, 1))
        synced = {
            ox.Triple(q.subject, q.predicate, q.object)
            for q in ox.parse(
                canonical_ntriples(expand_to_ntriples(doc, 1)), format=ox.RdfFormat.N_TRIPLES
            )
        }
        assert synced <= turtle_triples


def test_every_corpus_record_serializes_as_published() -> None:
    for _, record in iter_corpus():
        assert parse(to_turtle(record, 1))


def test_turtle_rejects_bad_iris() -> None:
    with pytest.raises(InvalidIRIError):
        to_turtle({**PUBLIC, "classified_as": [{"id": "https://example.org/a b"}]}, 1)


def test_context_prefixes_come_from_the_pinned_context() -> None:
    prefixes = context_prefixes(1)
    assert prefixes["crm"] == CRM
    assert prefixes["la"] == "https://linked.art/ns/terms/"
    assert all(not key.startswith("@") for key in prefixes)
