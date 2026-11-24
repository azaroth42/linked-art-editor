"""Tests for ``lae.bootstrap.records``: one input document to its rows, or a reported failure."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import orjson
import pytest

from lae.api.records import record_etag
from lae.bootstrap import records as records_module
from lae.bootstrap.common import (
    EXPANSION_FAILED,
    INVALID_ID,
    NO_RECORD_URI,
    NOT_A_RECORD,
    PREPARE_FAILED,
    UNKNOWN_TYPE,
)
from lae.bootstrap.records import (
    ParsedItem,
    PreparedRecord,
    build_batch,
    parse,
    parse_batch,
    prepare,
)
from lae.bootstrap.source import SourceItem, iter_source
from lae.corpus import CORPUS_DIR
from lae.ids import is_internal_node_iri, new_uuid7, record_uri
from lae.projection import canonical_json, strip_internal_iris
from lae.rdf import expand_to_ntriples
from lae.validation.schema import validate_schema
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
    record, failure = prepare(item(doc), BASE_URI, 1, "en")
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


def test_a_corpus_record_is_reminted_and_gets_node_iris_an_etag_and_labels() -> None:
    source = corpus(BROWN_TEA_POT)
    record = prepared(source)
    data = json.loads(record.data)
    assert record.id.version == 7
    assert record.uri == record_uri(record.id, base_uri=BASE_URI) == data["id"]
    assert (record.source_uri, record.alias) == (source["id"], source["id"])
    assert record.type == "HumanMadeObject"
    assert record.findings == []
    nodes = embedded_nodes(data)
    assert nodes and all(isinstance(node.get("id"), str) for node in nodes)
    minted = [node["id"] for node in nodes if is_internal_node_iri(node["id"], record.uri)]
    assert minted
    # The public form is the source with the new id and the source URI as an equivalent.
    public = strip_internal_iris(data, record.uri)
    own = {"id": source["id"], "type": "HumanMadeObject"}
    assert public == {**source, "id": record.uri, "equivalent": [*source["equivalent"], own]}
    assert record.data.encode() == canonical_json(data)
    assert record.etag == hashlib.blake2b(canonical_json(data), digest_size=16).hexdigest()
    assert record.etag == record_etag(data)
    assert json.loads(record.labels) == {"en": "The Brown Tea Pot"}
    assert record.default_label == "The Brown Tea Pot"
    assert record.primary_classification == "http://vocab.getty.edu/aat/300033618"
    assert record.source == "test.json"
    lines = expand_to_ntriples(data, 1)  # every embedded node has an IRI: no BlankNodeError
    equivalent = f"<{record.uri}> <https://linked.art/ns/terms/equivalent> <{source['id']}> ."
    assert equivalent in lines


def test_labels_in_every_language() -> None:
    record = prepared(corpus(NIGHT_WATCH))
    assert json.loads(record.labels) == {"en": "The Night Watch", "nl": "De Nachtwacht"}
    assert record.default_label == "The Night Watch"


def test_a_local_uri_is_kept_with_its_uuid_and_gets_no_alias() -> None:
    local_id = new_uuid7()
    local_uri = f"{BASE_URI}{local_id}"
    record = prepared({**corpus(BROWN_TEA_POT), "id": local_uri})
    assert (record.id, record.uri, record.source_uri, record.alias) == (
        local_id,
        local_uri,
        local_uri,
        None,
    )
    assert json.loads(record.data)["equivalent"] == corpus(BROWN_TEA_POT)["equivalent"]


def test_a_root_id_that_is_a_node_iri_of_a_local_record_is_rejected() -> None:
    """``lae.ids`` resolves such an IRI to the record that owns the node, so as an alias it would
    name two records, and the second pass could replace the owner's own node id by it."""
    node = f"{BASE_URI}{new_uuid7()}/node/{new_uuid7()}"
    record, failure = prepare(item({**corpus(BROWN_TEA_POT), "id": node}), BASE_URI, 1, "en")
    assert record is None and failure is not None
    assert (failure.code, failure.uri) == (NO_RECORD_URI, node)
    # Anything else under the base URI is an ordinary source URI.
    slug = prepared({**corpus(BROWN_TEA_POT), "id": f"{BASE_URI}object/7"})
    assert slug.alias == f"{BASE_URI}object/7"


def test_a_reminted_record_that_is_loaded_again_as_exported_is_unchanged() -> None:
    """Stored JSON-LD (with node IRIs) is valid input: nothing is minted or added twice."""
    record = prepared(corpus("linkedart/visual-nightwatch-1.json"))
    stored = json.loads(record.data)
    again = prepared(stored)
    assert json.loads(again.data) == stored
    assert (again.id, again.alias, again.etag) == (record.id, None, record.etag)


def test_node_iris_under_the_source_uri_move_with_the_record() -> None:
    source = corpus(BROWN_TEA_POT)
    node = f"{source['id']}/node/{new_uuid7()}"
    source["identified_by"][0]["id"] = node
    record = prepared(source)
    data = json.loads(record.data)
    assert data["identified_by"][0]["id"] == record.uri + node[len(source["id"]) :]
    assert all(
        source["id"] not in line or "equivalent" in line for line in expand_to_ntriples(data, 1)
    )


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (b"{not json", NOT_A_RECORD),
        (b"[1, 2]", NOT_A_RECORD),
        (b'{"type": "Type", "_label": "x"}', NO_RECORD_URI),
        (b'{"id": "no-scheme", "type": "Type", "_label": "x"}', NO_RECORD_URI),
        (b'{"id": "urn:x:1", "type": "Thing", "_label": "x"}', UNKNOWN_TYPE),
        (b'{"id": "urn:x:1", "type": ["Type", "Set"], "_label": "x"}', UNKNOWN_TYPE),
        (b'{"id": "urn:x:1", "_label": "x"}', UNKNOWN_TYPE),
    ],
)
def test_items_that_are_not_records(text: bytes, code: str) -> None:
    record, failure = prepare(SourceItem("in.jsonl:3", text), BASE_URI, 1, "en")
    assert record is None
    assert failure is not None and failure.code == code and failure.source == "in.jsonl:3"


def test_an_unrepairable_id_below_the_root_is_reported() -> None:
    doc = {**corpus(BROWN_TEA_POT), "member_of": [{"id": "https:/no-host", "type": "Set"}]}
    record, failure = prepare(item(doc), BASE_URI, 1, "en")
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
    parsed = parse(item(doc))
    assert isinstance(parsed, ParsedItem) and parsed.repaired_ids == 1
    record = prepared(doc)
    assert record.source_uri == corpus(BROWN_TEA_POT)["id"]


def test_a_schema_failure_is_loaded_and_flagged() -> None:
    record = prepared(corpus(YUAG_OBJECT))
    assert [code for code, _ in record.findings] == ["LAE-SCHEMA-type"]
    params = json.loads(record.findings[0][1])
    assert params["source"] == "bootstrap"
    assert params["count"] == 1
    assert [entry["path"] for entry in params["diagnostics"]] == ["/used_for"]
    assert params["diagnostics"][0]["params"]["validator_value"] == "array"
    # The findings are exactly what validation says about the stored record.
    stored = json.loads(record.data)
    assert [d.code for d in validate_schema(stored, record.uri)] == ["LAE-SCHEMA-type"]
    assert expand_to_ntriples(stored, 1, record_uri=record.uri)


def test_findings_are_grouped_by_code_with_a_count() -> None:
    doc = corpus(BROWN_TEA_POT)
    doc["member_of"] = {"id": "urn:x:set", "type": "Set"}
    doc["shows"] = {"id": "urn:x:image", "type": "VisualItem"}
    del doc["_label"]
    record = prepared(doc)
    by_code = {code: json.loads(params) for code, params in record.findings}
    assert set(by_code) == {"LAE-SCHEMA-required", "LAE-SCHEMA-type"}
    assert by_code["LAE-SCHEMA-type"]["count"] == 2
    assert [entry["path"] for entry in by_code["LAE-SCHEMA-type"]["diagnostics"]] == [
        "/member_of",
        "/shows",
    ]
    assert by_code["LAE-SCHEMA-required"]["diagnostics"][0]["params"]["missing"] == ["_label"]


def test_a_record_that_cannot_be_expanded_is_rejected() -> None:
    doc = corpus(BROWN_TEA_POT)
    doc["produced_by"]["timespan"] = {
        "type": "TimeSpan",
        "begin_of_the_begin": "c. 1850",
        "end_of_the_end": "1850",
    }
    record, failure = prepare(item(doc), BASE_URI, 1, "en")
    assert record is None and failure is not None
    assert (failure.code, failure.uri, failure.stage) == (EXPANSION_FAILED, doc["id"], "load")
    assert failure.errors[0].startswith("InvalidLiteralError: ")


def test_an_unexpected_error_rejects_one_record_not_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(records_module, "assign_node_iris", explode)
    record, failure = prepare(item(corpus(BROWN_TEA_POT)), BASE_URI, 1, "en")
    assert record is None and failure is not None
    assert (failure.code, failure.errors) == (PREPARE_FAILED, ["RuntimeError: boom"])


def test_every_corpus_file_loads_and_the_known_schema_failures_are_flagged() -> None:
    """Zero records lost: all 276 corpus files expand, so all are loaded (ADR-0030)."""
    parsed, not_records = parse_batch(iter_source(CORPUS_DIR))
    assert not_records == []
    batch = build_batch(parsed, BASE_URI, 1, "en")
    assert batch.failures == []
    assert len(batch.records) == 276
    assert {record.source for record in batch.records if record.findings} == KNOWN_FAILURES
    assert len({record.uri for record in batch.records}) == 276
    for record in batch.records:
        assert record.alias == record.source_uri
        lines = expand_to_ntriples(orjson.loads(record.data), 1, record_uri=record.uri)
        assert lines
