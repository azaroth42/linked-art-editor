"""Unit tests for the export worker and the sidecar label quads (no database)."""

from __future__ import annotations

import gzip
import json

import pytest

from lae.bootstrap.common import EXPANSION_FAILED, INVALID_ID, NO_RECORD_URI
from lae.bootstrap.export import (
    RDF_TYPE,
    RDFS_LABEL,
    class_iri,
    expand_batch,
    sidecar_label_quads,
)
from lae.bootstrap.records import prepare
from lae.bootstrap.source import iter_source
from lae.corpus import CORPUS_DIR
from lae.rdf import expand_to_ntriples
from lae.rdf.canonical import is_canonical_line
from tests.bootstrap.conftest import BASE_URI

CRM = "http://www.cidoc-crm.org/cidoc-crm/"
AAT_PAINTING = "http://vocab.getty.edu/aat/300033618"
GRAPH = "<urn:graph:sidecar:aat>"


def stored_rows() -> list[tuple[str, str, int]]:
    """``(uri, data, context_version)`` of every corpus record the load would store."""
    rows = []
    for item in iter_source(CORPUS_DIR):
        record, _ = prepare(item, BASE_URI, 1, "en")
        if record is not None:
            rows.append((record.uri, record.data, 1))
    return rows


def test_the_batch_is_exactly_the_records_expansions() -> None:
    rows = stored_rows()
    batch = expand_batch(rows, compress=False)
    assert batch.failures == []
    assert batch.records == len(rows) == 276
    lines = batch.data.decode().splitlines()
    assert len(lines) == batch.triples
    expected: set[str] = set()
    for uri, data, version in rows:
        expected |= expand_to_ntriples(json.loads(data), version, record_uri=uri)
    assert set(lines) == expected
    assert all(is_canonical_line(line) for line in lines)


def test_each_record_contributes_sorted_lines() -> None:
    uri, data, version = stored_rows()[0]
    lines = expand_batch([(uri, data, version)], compress=False).data.decode().splitlines()
    assert lines == sorted(lines)


def test_compressed_batches_concatenate_into_one_gzip_stream() -> None:
    rows = stored_rows()
    first = expand_batch(rows[:10], compress=True)
    second = expand_batch(rows[10:20], compress=True)
    plain = expand_batch(rows[:20], compress=False)
    assert gzip.decompress(first.data + second.data) == plain.data


def test_records_that_cannot_be_expanded_are_reported_not_written() -> None:
    uri = "https://example.org/object/1"
    blank = {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": uri,
        "type": "HumanMadeObject",
        "identified_by": [{"type": "Name", "content": "no node IRI"}],
    }
    bad_id = {**blank, "id": "https:/no-host", "identified_by": []}
    bad_ref = {**blank, "identified_by": [], "member_of": [{"id": "https:/x", "type": "Set"}]}
    good_uri, good_data, _ = stored_rows()[0]
    batch = expand_batch(
        [
            (uri, json.dumps(blank), 1),
            ("https:/no-host", json.dumps(bad_id), 1),
            (uri, json.dumps(bad_ref), 1),
            (good_uri, good_data, 1),
        ],
        compress=False,
    )
    assert batch.records == 1
    assert [(f.code, f.uri) for f in batch.failures] == [
        (EXPANSION_FAILED, uri),
        (NO_RECORD_URI, "https:/no-host"),
        (INVALID_ID, uri),
    ]
    assert batch.failures[0].errors[0].startswith("BlankNodeError: ")
    assert uri.encode() not in batch.data


def test_class_iri_comes_from_the_pinned_context() -> None:
    assert class_iri("Type", 1) == CRM + "E55_Type"
    assert class_iri("Person", 1) == CRM + "E21_Person"
    assert class_iri("NoSuchClass", 1) is None


def test_sidecar_label_quads() -> None:
    quads = sidecar_label_quads(
        AAT_PAINTING, "Type", {"en": 'paintings "visual"', "pt-BR": "pinturas"}, "x", "aat", 1
    )
    assert quads == [
        f"<{AAT_PAINTING}> <{RDF_TYPE}> <{CRM}E55_Type> {GRAPH} .",
        f'<{AAT_PAINTING}> <{RDFS_LABEL}> "paintings \\"visual\\""@en {GRAPH} .',
        f'<{AAT_PAINTING}> <{RDFS_LABEL}> "pinturas"@pt-br {GRAPH} .',
    ]


def test_sidecar_row_without_labels_uses_the_default_label() -> None:
    quads = sidecar_label_quads(AAT_PAINTING, "Type", {}, "paintings", "aat", 1)
    assert quads[1] == f'<{AAT_PAINTING}> <{RDFS_LABEL}> "paintings" {GRAPH} .'


def test_a_language_key_that_is_not_a_tag_gives_a_plain_literal() -> None:
    quads = sidecar_label_quads(AAT_PAINTING, "Type", {"not a tag": "x"}, "x", "aat", 1)
    assert quads[1] == f'<{AAT_PAINTING}> <{RDFS_LABEL}> "x" {GRAPH} .'


@pytest.mark.parametrize(
    ("uri", "type_name", "source"),
    [
        ("http://vocab.getty.edu/aat/1 2 3", "Type", "aat"),
        (AAT_PAINTING, "NoSuchClass", "aat"),
        (AAT_PAINTING, "Type", "a b>"),
    ],
)
def test_unwritable_sidecar_rows_raise(uri: str, type_name: str, source: str) -> None:
    with pytest.raises(ValueError):
        sidecar_label_quads(uri, type_name, {"en": "x"}, "x", source, 1)
