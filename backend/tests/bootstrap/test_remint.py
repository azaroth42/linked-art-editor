"""Tests for ``lae.bootstrap.remint``: re-minting a record and rewriting references (ADR-0030)."""

from __future__ import annotations

import copy
from typing import Any

from lae.bootstrap.remint import (
    local_record_id,
    reference_targets,
    remint,
    rewrite_references,
)
from lae.ids import new_uuid7, node_iri, record_uri
from lae.rdf import expand_to_ntriples

BASE = "https://editor.example.org/id/"
SOURCE = "https://museum.example.org/object/1"
OTHER = "https://museum.example.org/person/7"


def new_uri() -> str:
    return record_uri(new_uuid7(), base_uri=BASE)


def source_record() -> dict[str, Any]:
    return {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": SOURCE,
        "type": "HumanMadeObject",
        "_label": "A pot",
        "identified_by": [{"type": "Name", "content": "A pot"}],
        "produced_by": {
            "type": "Production",
            "carried_out_by": [{"id": OTHER, "type": "Person", "_label": "A potter"}],
        },
    }


def test_only_the_exact_local_form_is_a_local_record_uri() -> None:
    record_id = new_uuid7()
    uri = record_uri(record_id, base_uri=BASE)
    assert local_record_id(uri, BASE) == record_id
    assert local_record_id(node_iri(uri), BASE) is None
    assert local_record_id(uri + "/", BASE) is None
    assert local_record_id(uri.upper(), BASE) is None
    assert local_record_id(SOURCE, BASE) is None


def test_remint_changes_the_root_id_and_appends_the_source_to_equivalent() -> None:
    source = source_record()
    before = copy.deepcopy(source)
    uri = new_uri()
    out = remint(source, SOURCE, uri)
    assert source == before  # the input is not modified
    assert out["id"] == uri
    assert out["equivalent"] == [{"id": SOURCE, "type": "HumanMadeObject"}]
    assert {key: value for key, value in out.items() if key not in ("id", "equivalent")} == {
        key: value for key, value in source.items() if key != "id"
    }


def test_remint_does_not_duplicate_an_equivalent_that_is_already_there() -> None:
    source = source_record()
    listed = {"id": SOURCE, "type": "HumanMadeObject", "_label": "Our old page"}
    source["equivalent"] = [{"id": "https://other.example/1", "type": "HumanMadeObject"}, listed]
    uri = new_uri()
    out = remint(source, SOURCE, uri)
    assert out["equivalent"] == source["equivalent"]
    assert remint(remint(source, SOURCE, uri), SOURCE, uri) == out


def test_remint_turns_a_single_equivalent_object_into_a_list() -> None:
    source = source_record()
    source["equivalent"] = {"id": "https://other.example/1", "type": "HumanMadeObject"}
    out = remint(source, SOURCE, new_uri())
    assert out["equivalent"] == [
        {"id": "https://other.example/1", "type": "HumanMadeObject"},
        {"id": SOURCE, "type": "HumanMadeObject"},
    ]


def test_remint_moves_node_iris_and_self_references_to_the_new_uri() -> None:
    """An export of an editor (or the S6 generator) carries node IRIs under the source URI."""
    node = f"{SOURCE}/node/{new_uuid7()}"
    foreign_node = f"{OTHER}/node/{new_uuid7()}"
    source = source_record()
    source["identified_by"] = [{"id": node, "type": "Name", "content": "A pot"}]
    source["part_of"] = [{"id": SOURCE, "type": "HumanMadeObject", "_label": "itself"}]
    source["shows"] = [{"id": foreign_node, "type": "VisualItem"}]
    source["subject_of"] = [
        {
            "type": "LinguisticObject",
            "digitally_carried_by": [
                {"type": "DigitalObject", "access_point": [{"id": SOURCE, "type": "DigitalObject"}]}
            ],
        }
    ]
    uri = new_uri()
    out = remint(source, SOURCE, uri)
    assert out["identified_by"][0]["id"] == uri + node[len(SOURCE) :]
    assert out["part_of"] == [{"id": uri, "type": "HumanMadeObject", "_label": "itself"}]
    assert out["shows"] == [{"id": foreign_node, "type": "VisualItem"}]
    # An access point is an address, not a reference to the record: it is kept.
    access = out["subject_of"][0]["digitally_carried_by"][0]["access_point"]
    assert access == [{"id": SOURCE, "type": "DigitalObject"}]
    assert out["equivalent"] == [{"id": SOURCE, "type": "HumanMadeObject"}]


def test_rewrite_replaces_aliases_at_any_depth_and_counts_them() -> None:
    uri, other_uri = new_uri(), new_uri()
    document = remint(source_record(), SOURCE, uri)
    document["member_of"] = [{"id": OTHER, "type": "Set"}, {"id": "urn:x:unknown", "type": "Set"}]
    document["equivalent"].append({"id": OTHER, "type": "HumanMadeObject"})
    before = copy.deepcopy(document)
    out, count = rewrite_references(document, uri, {OTHER: other_uri, SOURCE: uri})
    assert document == before
    assert count == 3
    assert out["produced_by"]["carried_out_by"] == [
        {"id": other_uri, "type": "Person", "_label": "A potter"}
    ]
    assert out["member_of"] == [
        {"id": other_uri, "type": "Set"},
        {"id": "urn:x:unknown", "type": "Set"},
    ]
    # Its own source URI stays in equivalent; another record's alias there is rewritten.
    assert out["equivalent"] == [
        {"id": SOURCE, "type": "HumanMadeObject"},
        {"id": other_uri, "type": "HumanMadeObject"},
    ]
    assert out["id"] == uri and out["@context"] == document["@context"]


def test_rewrite_never_introduces_the_records_own_uri() -> None:
    uri = new_uri()
    document = {
        "id": uri,
        "type": "Set",
        "member_of": [{"id": SOURCE, "type": "Set"}],
        "equivalent": [{"id": SOURCE, "type": "Set"}],
    }
    out, count = rewrite_references(document, uri, {SOURCE: uri})
    assert (out, count) == (document, 0)


def test_rewrite_leaves_context_and_access_points_alone() -> None:
    uri, other_uri = new_uri(), new_uri()
    document = {
        "@context": {"id": OTHER},
        "id": uri,
        "type": "DigitalObject",
        "access_point": [{"id": OTHER, "type": "DigitalObject"}],
        "digitally_shows": [{"@id": OTHER, "type": "VisualItem"}],
    }
    out, count = rewrite_references(document, uri, {OTHER: other_uri})
    assert count == 1
    assert out["@context"] == {"id": OTHER}
    assert out["access_point"] == [{"id": OTHER, "type": "DigitalObject"}]
    assert out["digitally_shows"] == [{"@id": other_uri, "type": "VisualItem"}]


def test_reference_targets_leave_out_own_node_iris_and_unrewritten_keys() -> None:
    uri = new_uri()
    document = {
        "@context": {"id": "urn:context"},
        "id": uri,
        "type": "HumanMadeObject",
        "identified_by": [{"id": node_iri(uri), "type": "Name", "content": "x"}],
        "produced_by": {
            "id": node_iri(uri),
            "type": "Production",
            "carried_out_by": [{"id": OTHER, "type": "Person"}],
        },
        "access_point": [{"id": "https://files.example/1"}],
        "equivalent": [{"id": SOURCE, "type": "HumanMadeObject"}],
    }
    assert reference_targets(document, uri) == {OTHER, SOURCE}


def test_two_records_that_refer_to_each_other_resolve_and_still_expand() -> None:
    a_source, b_source = "https://museum.example.org/object/a", "https://museum.example.org/set/b"
    a_uri, b_uri = new_uri(), new_uri()
    a = {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": a_source,
        "type": "HumanMadeObject",
        "_label": "A",
        "member_of": [{"id": b_source, "type": "Set", "_label": "B"}],
    }
    b = {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": b_source,
        "type": "Set",
        "_label": "B",
        "members_exemplified_by": [{"id": a_source, "type": "HumanMadeObject", "_label": "A"}],
    }
    aliases = {a_source: a_uri, b_source: b_uri}
    a_out, a_count = rewrite_references(remint(a, a_source, a_uri), a_uri, aliases)
    b_out, b_count = rewrite_references(remint(b, b_source, b_uri), b_uri, aliases)
    assert (a_count, b_count) == (1, 1)
    assert a_out["member_of"][0]["id"] == b_uri
    assert b_out["members_exemplified_by"][0]["id"] == a_uri
    a_lines = expand_to_ntriples(a_out, 1, record_uri=a_uri)
    assert any(f"<{a_uri}>" in line and f"<{b_uri}>" in line for line in a_lines)
    assert any(f"<{a_source}>" in line and "equivalent" in line for line in a_lines)
    b_lines = expand_to_ntriples(b_out, 1, record_uri=b_uri)
    assert any(f"<{b_uri}>" in line and f"<{a_uri}>" in line for line in b_lines)
