"""Tests for ``lae.bootstrap.records``: one input document to its rows, or a reported failure."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import UUID

import orjson
import pytest

from lae.api.records import record_etag
from lae.bootstrap.common import (
    INVALID_ID,
    NO_RECORD_URI,
    NOT_A_RECORD,
    SCHEMA_INVALID,
    UNKNOWN_TYPE,
)
from lae.bootstrap.records import (
    PreparedRecord,
    prepare,
    prepare_batch,
    schema_errors,
)
from lae.bootstrap.source import SourceItem, iter_source
from lae.corpus import CORPUS_DIR
from lae.ids import is_internal_node_iri, new_uuid7
from lae.projection import canonical_json, strip_internal_iris
from lae.rdf import expand_to_ntriples
from tests.bootstrap.conftest import BASE_URI, BROWN_TEA_POT, NIGHT_WATCH, YUAG_OBJECT, corpus

KNOWN_FAILURES = set(
    re.findall(
        r"^\| `([^`]+\.json)` \|",
        (CORPUS_DIR / "KNOWN_FAILURES.md").read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
)


def item(doc: Any, source: str = "test.json") -> SourceItem:
    return SourceItem(source, json.dumps(doc).encode())


def prepared(doc: dict[str, Any]) -> PreparedRecord:
    record, failure, _ = prepare(item(doc), BASE_URI)
    assert failure is None, failure
    assert record is not None
    return record


def embedded_nodes(value: Any, *, root: bool = True) -> list[dict[str, Any]]:
    """Every object below the root that has a ``type`` and is not a reference."""
    found: list[dict[str, Any]] = []
    if isinstance(value, list):
        for entry in value:
            found.extend(embedded_nodes(entry, root=False))
    elif isinstance(value, dict):
        if not root and "type" in value and not value.keys() <= {"id", "type", "_label"}:
            found.append(value)
        for key, entry in value.items():
            if key != "@context":
                found.extend(embedded_nodes(entry, root=False))
    return found


def test_a_corpus_record_gets_node_iris_an_etag_and_labels() -> None:
    source = corpus(BROWN_TEA_POT)
    record = prepared(source)
    data = json.loads(record.data)
    assert record.uri == source["id"]
    assert record.type == "HumanMadeObject"
    nodes = embedded_nodes(data)
    assert nodes and all(isinstance(node.get("id"), str) for node in nodes)
    minted = [node["id"] for node in nodes if is_internal_node_iri(node["id"], record.uri)]
    assert minted
    assert strip_internal_iris(data, record.uri) == source
    assert record.data.encode() == canonical_json(data)
    assert record.etag == hashlib.blake2b(canonical_json(data), digest_size=16).hexdigest()
    assert json.loads(record.labels) == {"en": "The Brown Tea Pot"}
    assert record.default_label == "The Brown Tea Pot"
    assert record.primary_classification == "http://vocab.getty.edu/aat/300033618"
    assert record.source == "test.json"
    expand_to_ntriples(data, 1)  # every embedded node has an IRI: no BlankNodeError


def test_labels_in_every_language() -> None:
    record = prepared(corpus(NIGHT_WATCH))
    assert json.loads(record.labels) == {"en": "The Night Watch", "nl": "De Nachtwacht"}
    assert record.default_label == "The Night Watch"


def test_the_etag_is_the_one_publish_computes() -> None:
    record = prepared(corpus(BROWN_TEA_POT))
    assert record.etag == record_etag(json.loads(record.data))


def test_a_local_uri_keeps_its_uuid_and_a_foreign_one_gets_a_uuidv7() -> None:
    local_id = new_uuid7()
    local = {**corpus(BROWN_TEA_POT), "id": f"{BASE_URI}{local_id}"}
    assert prepared(local).id == local_id
    foreign = prepared(corpus(BROWN_TEA_POT))
    assert isinstance(foreign.id, UUID) and foreign.id.version == 7


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (b"{not json", NOT_A_RECORD),
        (b"[1, 2]", NOT_A_RECORD),
        (b'{"type": "Type", "_label": "x"}', NO_RECORD_URI),
        (b'{"id": "no-scheme", "type": "Type", "_label": "x"}', NO_RECORD_URI),
        (b'{"id": "urn:x:1", "type": "Thing", "_label": "x"}', UNKNOWN_TYPE),
    ],
)
def test_items_that_are_not_records(text: bytes, code: str) -> None:
    record, failure, _ = prepare(SourceItem("in.jsonl:3", text), BASE_URI)
    assert record is None
    assert failure is not None and failure.code == code and failure.source == "in.jsonl:3"


def test_an_unrepairable_id_below_the_root_is_reported() -> None:
    doc = {**corpus(BROWN_TEA_POT), "member_of": [{"id": "https:/no-host", "type": "Set"}]}
    record, failure, _ = prepare(item(doc), BASE_URI)
    assert record is None
    assert failure is not None
    assert (failure.code, failure.uri, failure.errors) == (
        INVALID_ID,
        doc["id"],
        ["https:/no-host"],
    )


def test_repairable_ids_are_repaired_and_counted() -> None:
    doc = corpus(BROWN_TEA_POT)
    doc["id"] = doc["id"] + " "
    record, failure, repaired = prepare(item(doc), BASE_URI)
    assert failure is None and record is not None
    assert record.uri == corpus(BROWN_TEA_POT)["id"]
    assert repaired == 1


def test_a_schema_failure_lists_the_errors() -> None:
    record, failure, _ = prepare(item(corpus(YUAG_OBJECT)), BASE_URI)
    assert record is None and failure is not None
    assert failure.code == SCHEMA_INVALID
    assert failure.errors == ["/used_for type: is not of type 'array'"]


def test_schema_validation_sees_the_public_form_without_node_iris() -> None:
    """Many schemas forbid ``id`` on embedded nodes (129 of the 184 valid corpus records would
    fail with node IRIs), so validation runs on the record with internal IRIs stripped."""
    record = prepared(corpus("linkedart/visual-nightwatch-1.json"))
    stored = json.loads(record.data)
    assert schema_errors(stored) == [
        "/referred_to_by/0 additionalProperties: Additional properties are not allowed "
        "('id' was unexpected)"
    ]
    # Input that already carries node IRIs (an export, the S6 generator) loads unchanged.
    again = prepared(stored)
    assert json.loads(again.data) == stored
    assert again.etag == record.etag


def test_every_corpus_file_loads_unless_it_is_a_known_schema_failure() -> None:
    batch = prepare_batch(iter_source(CORPUS_DIR), BASE_URI)
    assert {failure.source for failure in batch.failures} == KNOWN_FAILURES
    assert {failure.code for failure in batch.failures} == {SCHEMA_INVALID}
    assert len(batch.records) == 244 - len(KNOWN_FAILURES)
    for record in batch.records:
        lines = expand_to_ntriples(orjson.loads(record.data), 1, record_uri=record.uri)
        assert lines
