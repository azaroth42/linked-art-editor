"""Public RDF serializations of a record.

``to_turtle`` writes whatever JSON-LD it is given: the caller decides what is public. For the
public Turtle representation that is the record after ``strip_internal_iris`` (Step 1.4), whose
embedded nodes are then blank nodes; ownership stripping does not apply, because this output is
read by people and never synced to QLever. IRIs and literals are checked exactly as for
``expand_to_ntriples``, with the two repairs of ADR-0028. Turtle is stricter than N-Triples in
one respect: pyoxigraph refuses an IRI that N-Triples can hold but RFC 3987 does not allow (a
malformed percent escape as in ``100%wool``, a ``[`` in a path, U+007F). ``to_turtle`` raises
``InvalidIRIError`` for one, as for any other IRI it cannot write.
"""

from __future__ import annotations

from typing import Any

import pyoxigraph as ox

from lae.rdf.context import load_context
from lae.rdf.expand import InvalidIRIError, literal_parts, rdf_triples

_NAMESPACE_ENDINGS = ("/", "#")


def context_prefixes(context_version: int) -> dict[str, str]:
    """Return the namespace prefixes the pinned context declares (``crm``, ``la``, ...)."""
    context = load_context(context_version)["@context"]
    return {
        term: value
        for term, value in sorted(context.items())
        if isinstance(value, str)
        and not term.startswith("@")
        and value.endswith(_NAMESPACE_ENDINGS)
    }


def _node(term: dict[str, Any]) -> ox.NamedNode | ox.BlankNode:
    value: str = term["value"]
    if term["type"] == "blank node":
        return ox.BlankNode(value.removeprefix("_:"))
    return _named(value)


def _named(iri: str) -> ox.NamedNode:
    try:
        return ox.NamedNode(iri)
    except ValueError as error:
        raise InvalidIRIError(f"not an IRI Turtle can hold: {iri!r} ({error})") from error


def _object(term: dict[str, Any]) -> ox.NamedNode | ox.BlankNode | ox.Literal:
    if term["type"] != "literal":
        return _node(term)
    lexical, datatype, language = literal_parts(term)
    if language is not None:
        return ox.Literal(lexical, language=language)
    if datatype is not None:
        return ox.Literal(lexical, datatype=_named(datatype))
    return ox.Literal(lexical)


def to_turtle(jsonld: dict[str, Any], context_version: int) -> str:
    """Return ``jsonld`` as Turtle, with the pinned context's prefixes and a stable triple order."""
    triples = {
        ox.Triple(
            _node(triple["subject"]),
            _named(triple["predicate"]["value"]),
            _object(triple["object"]),
        )
        for triple in rdf_triples(jsonld, context_version, allow_blank_nodes=True)
    }
    data = ox.serialize(
        sorted(triples, key=str),
        format=ox.RdfFormat.TURTLE,
        prefixes=context_prefixes(context_version),
    )
    assert data is not None
    return data.decode("utf-8")
