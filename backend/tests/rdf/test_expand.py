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

from lae.rdf.canonical import canonical_ntriples, is_canonical_line
from lae.rdf.context import document_loader
from lae.rdf.diff import diff_triples
from lae.rdf.expand import (
    BlankNodeError,
    ExpansionError,
    InvalidIRIError,
    InvalidLiteralError,
    OwnershipError,
    expand_to_ntriples,
    owned_subjects,
    strip_stub_attributes,
)

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
    owned = owned_subjects(record(), RECORD)
    assert owned == {RECORD, NAME, PRODUCTION, TIMESPAN}
    assert {subject_of(line) for line in expand_to_ntriples(record(), 1)} == owned


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
    assert owned_subjects(doc, RECORD) == {RECORD, inner}


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


def test_a_node_iri_that_is_not_a_uuid7_is_foreign() -> None:
    lookalike = RECORD + "/node/not-a-uuid"
    doc = {
        "@context": CONTEXT,
        "id": RECORD,
        "type": "HumanMadeObject",
        "identified_by": [{"id": lookalike, "type": "Name", "content": "x"}],
    }
    assert expand_to_ntriples(doc, 1) == {
        nt(RECORD, RDF_TYPE, CRM + "E22_Human-Made_Object"),
        nt(RECORD, CRM + "P1_is_identified_by", lookalike),
    }


def test_root_id_must_be_the_record_uri() -> None:
    with pytest.raises(OwnershipError):
        strip_stub_attributes(record(), OTHER_RECORD)
    with pytest.raises(OwnershipError):
        expand_to_ntriples(record(), 1, record_uri=OTHER_RECORD)


@pytest.mark.parametrize(
    "smuggle",
    [
        # A scoped context that renames @id hides a foreign subject from the JSON-level strip.
        {
            "@context": {"ident": "@id"},
            "ident": SIDECAR,
            "type": "Type",
            "_label": "smuggled",
        },
        # A reverse property makes the foreign entity the subject of the link.
        {
            "id": NAME,
            "type": "Name",
            "@reverse": {f"{CRM}P2_has_type": {"id": SIDECAR}},
        },
    ],
    ids=["aliased-id", "reverse"],
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


def _reachable_owned_triples(triples: set[ox.Triple], record_uri: str) -> set[ox.Triple]:
    """Independent statement of the ownership rule, on the unstripped graph.

    Keep the triples of every owned subject that can be reached from the record by following
    links through owned subjects only.
    """
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
    assert ours == _reachable_owned_triples(_pyld_reference_triples(doc), record_uri)


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
