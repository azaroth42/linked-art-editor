"""SPARQL update builders: output parses, and applying a diff turns the old triples into the new."""

from __future__ import annotations

import copy
from typing import Any

import pyoxigraph as ox
import pytest

from lae.corpus import iter_corpus
from lae.rdf.canonical import canonical_ntriples
from lae.rdf.diff import diff_triples
from lae.rdf.expand import expand_to_ntriples, owned_subjects
from lae.sparql.updates import build_delete_where_subjects, build_update
from tests.rdf.node_iris import assign_node_iris

S = "https://example.org/id/1"
A = f'<{S}> <https://example.org/p> "one" .'
B = f"<{S}> <https://example.org/p> <https://example.org/b> ."
C = f'<{S}/node/x> <https://example.org/p> "é 日本"@en .'
D = f'<{S}> <https://example.org/q> "5"^^<http://www.w3.org/2001/XMLSchema#integer> .'
NASTY = (
    'quote " backslash \\ tab \t newline \n return \r nul \x00 bell \x07 del \x7f '
    "} ; DROP ALL ; <x>"
)


def store_with(lines: frozenset[str] | set[str]) -> ox.Store:
    store = ox.Store()
    store.load(canonical_ntriples(lines), format=ox.RdfFormat.N_TRIPLES)
    return store


def triples_in(store: ox.Store) -> set[ox.Triple]:
    assert all(quad.graph_name == ox.DefaultGraph() for quad in store)
    return {ox.Triple(q.subject, q.predicate, q.object) for q in store}


def triples_of(lines: frozenset[str] | set[str]) -> set[ox.Triple]:
    return {
        ox.Triple(q.subject, q.predicate, q.object)
        for q in ox.parse(canonical_ntriples(lines), format=ox.RdfFormat.N_TRIPLES)
    }


def test_build_update_text() -> None:
    assert build_update([B, A, A], [D]) == (
        f"DELETE DATA {{\n{A}\n{B}\n}} ;\nINSERT DATA {{\n{D}\n}}"
    )
    assert build_update([A], []) == f"DELETE DATA {{\n{A}\n}}"
    assert build_update([], [A]) == f"INSERT DATA {{\n{A}\n}}"
    assert build_update([], []) == ""


def test_build_update_accepts_any_iterable_and_is_order_independent() -> None:
    assert build_update(iter([A, B]), frozenset({C, D})) == build_update({B, A}, [D, C])


@pytest.mark.parametrize(
    ("delete", "insert"),
    [([A, B], [C, D]), ([A], []), ([], [C]), ([], [])],
)
def test_build_update_parses_and_applies(delete: list[str], insert: list[str]) -> None:
    store = store_with({A, B})
    store.update(build_update(delete, insert))
    assert triples_in(store) == triples_of(({A, B} - set(delete)) | set(insert))


@pytest.mark.parametrize(
    "line",
    [
        f'{A} }} ; DROP ALL ; INSERT DATA {{ <urn:x> <urn:p> "pwned" .',
        f"{A}\n{B}",
        f'<{S}> <https://example.org/p> "unterminated .',
        f'<{S}> <https://example.org/p> "x" . # }} ; DROP ALL',
        f'<{S}> <https://example.org/p> "\\u0022 }} ; DROP ALL ; INSERT DATA {{ <urn:x> <urn:p> "'
        '\\u0022" .',
        f"_:b0 <https://example.org/p> <{S}> .",
        f"<{S}> <https://example.org/p> ?o .",
        "",
    ],
)
def test_build_update_refuses_anything_but_canonical_lines(line: str) -> None:
    with pytest.raises(ValueError):
        build_update([line], [])
    with pytest.raises(ValueError):
        build_update([], [line])


def test_build_update_refuses_a_triple_on_both_sides() -> None:
    with pytest.raises(ValueError):
        build_update([A, B], [B])


def record(title: str) -> dict[str, Any]:
    record_uri = "https://editor.example.org/id/0192f0c1-0000-7000-8000-000000000001"
    return {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": record_uri,
        "type": "HumanMadeObject",
        "identified_by": [
            {
                "id": record_uri + "/node/0192f0c1-0000-7000-8000-0000000000a1",
                "type": "Name",
                "content": title,
            }
        ],
        "classified_as": [{"id": "http://vocab.getty.edu/aat/300033618", "type": "Type"}],
    }


@pytest.mark.parametrize("new_title", ["The Night Watch", NASTY, "", "日本 \U0001f600"])
def test_applying_the_diff_of_an_edit_yields_exactly_the_new_triples(new_title: str) -> None:
    old = expand_to_ntriples(record("Night Watch"), 1)
    new = expand_to_ntriples(record(new_title), 1)
    to_delete, to_insert = diff_triples(old, new)
    assert len(to_delete) == len(to_insert) == 1
    # Other records' triples in the same graph must be untouched.
    bystander = '<http://vocab.getty.edu/aat/300033618> <https://example.org/p> "Painting" .'
    store = store_with(old | {bystander})
    store.update(build_update(to_delete, to_insert))
    assert triples_in(store) == triples_of(new | {bystander})
    # And back again.
    store.update(build_update(*diff_triples(new, old)))
    assert triples_in(store) == triples_of(old | {bystander})


def test_applying_corpus_diffs_yields_exactly_the_new_triples() -> None:
    """Each corpus record, edited by dropping half of its top-level keys, syncs exactly."""
    checked = 0
    for _, raw in iter_corpus():
        doc = assign_node_iris(raw, raw["id"])
        edited = copy.deepcopy(doc)
        for key in [k for k in edited if k not in ("@context", "id", "type")][::2]:
            del edited[key]
        old = expand_to_ntriples(doc, 1)
        new = expand_to_ntriples(edited, 1)
        store = store_with(old)
        update = build_update(*diff_triples(old, new))
        if update:
            store.update(update)
            checked += 1
        assert triples_in(store) == triples_of(new)
    assert checked > 100


def test_build_delete_where_subjects_text() -> None:
    assert build_delete_where_subjects([f"{S}/node/x", S, S]) == (
        f"DELETE WHERE {{ <{S}> ?p ?o }} ;\nDELETE WHERE {{ <{S}/node/x> ?p ?o }}"
    )
    assert build_delete_where_subjects([]) == ""


def test_build_delete_where_subjects_removes_only_those_subjects() -> None:
    other = '<https://example.org/id/2> <https://example.org/p> "keep" .'
    incoming = f"<https://example.org/id/2> <https://example.org/p> <{S}> ."
    store = store_with({A, B, C, D, other, incoming})
    store.update(build_delete_where_subjects([S, f"{S}/node/x"]))
    assert triples_in(store) == triples_of({other, incoming})


def test_delete_where_over_owned_subjects_then_insert_resyncs_a_record() -> None:
    """ADR-0007: whatever stale triples the owned subjects had, the record ends up exact."""
    doc = record("Night Watch")
    record_uri = doc["id"]
    new = expand_to_ntriples(doc, 1)
    stale = {
        f'<{record_uri}> <https://example.org/old-predicate> "from an older expander" .',
        f'<{doc["identified_by"][0]["id"]}> <https://example.org/old-predicate> "stale" .',
    }
    bystander = '<http://vocab.getty.edu/aat/300033618> <https://example.org/p> "Painting" .'
    store = store_with(new | stale | {bystander})
    store.update(build_delete_where_subjects(owned_subjects(doc, record_uri)))
    store.update(build_update([], new))
    assert triples_in(store) == triples_of(new | {bystander})


@pytest.mark.parametrize(
    "subject",
    ["", "https://x/a> ?p ?o } ; DROP ALL ; DELETE WHERE { <b", "https://x/a b", "https://x/a\nb"],
)
def test_build_delete_where_subjects_refuses_bad_iris(subject: str) -> None:
    with pytest.raises(ValueError):
        build_delete_where_subjects([S, subject])
