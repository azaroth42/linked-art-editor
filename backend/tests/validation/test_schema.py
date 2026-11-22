"""JSON Schema diagnostics (``lae.validation.schema``)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from jsonschema import Draft7Validator

from lae.corpus import CORPUS_DIR, corpus_files, load_record
from lae.validation.diagnostics import Diagnostic
from lae.validation.schema import (
    REPORTED_KEYWORDS,
    schema_codes,
    schema_for_type,
    validate_schema,
    validator_for_record,
)
from lae.validation.schema_map import SCHEMA_BASE_URI, SCHEMA_BY_TYPE, SCHEMA_DIR
from tests.validation.conftest import SHAPE_CASES, base, found, with_node_iris

KNOWN_FAILURES = set(
    re.findall(
        r"^\| `([^`]+\.json)` \|",
        (CORPUS_DIR / "KNOWN_FAILURES.md").read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
)
PROVENANCE = {
    "@context": "https://linked.art/ns/v1/linked-art.json",
    "id": "https://example.org/activity/1",
    "type": "Activity",
    "_label": "Sale",
    "classified_as": [
        {"id": "http://vocab.getty.edu/aat/300055863", "type": "Type", "_label": "Provenance"}
    ],
}


def only(items: list[Diagnostic]) -> Diagnostic:
    assert len(items) == 1, found(items)
    return items[0]


@pytest.mark.parametrize("name", sorted(SHAPE_CASES["bases"]))
def test_base_records_are_schema_valid(name: str) -> None:
    assert validate_schema(base(name)) == []


def test_internal_node_iris_are_stripped_before_validating() -> None:
    record = with_node_iris(base())
    assert record["identified_by"][0]["id"].startswith("https://example.org/object/1/node/")
    assert validate_schema(record) == []
    # With another record URI they are not this record's node IRIs, and the schema sees them.
    assert validate_schema(record, "https://example.org/object/2") != []


def test_input_is_not_modified() -> None:
    record = with_node_iris(base())
    before = json.dumps(record, sort_keys=True)
    validate_schema(record)
    assert json.dumps(record, sort_keys=True) == before


def test_required_lists_every_missing_property_once() -> None:
    record = base()
    del record["_label"]
    del record["@context"]
    item = only(validate_schema(record))
    assert (item.code, item.path, item.severity) == ("LAE-SCHEMA-required", "", "violation")
    assert sorted(item.params["missing"]) == ["@context", "_label"]
    assert "_label" in item.params["validator_value"]
    assert item.params["instance_summary"]["type"] == "HumanMadeObject"
    assert item.params["instance_summary"]["id"] == "https://example.org/object/1"
    assert "_label" in item.message_en and "@context" in item.message_en


def test_path_is_a_json_pointer_to_the_failing_instance() -> None:
    record = base("HumanMadeObject", ["/identified_by/2/content", 1990])
    items = validate_schema(record)
    assert items, "a number as content must fail"
    assert all(item.severity == "violation" for item in items)
    assert all(item.code.startswith("LAE-SCHEMA-") for item in items)
    paths = {item.path for item in items}
    causes = {item.params["cause"]["path"] for item in items if "cause" in item.params}
    assert any(path.startswith("/identified_by/2") for path in paths | causes), found(items)


def test_any_of_names_the_error_in_the_alternative_the_type_selects() -> None:
    record = base("HumanMadeObject", ["/identified_by/2/content", 1990])
    causes = [item.params["cause"] for item in validate_schema(record) if "cause" in item.params]
    assert {"code": "LAE-SCHEMA-type", "path": "/identified_by/2/content"} in causes


def test_additional_properties_names_the_extra_keys() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"a": {}},
        "patternProperties": {"^x-": {}},
        "additionalProperties": False,
    }
    from lae.validation.schema import _diagnostics

    errors = Draft7Validator(schema).iter_errors({"a": 1, "x-ok": 2, "b": 3, "c": 4})
    item = only(list(_diagnostics(errors)))
    assert item.code == "LAE-SCHEMA-additionalProperties"
    assert item.params["extra"] == ["b", "c"]
    assert item.params["validator_value"] is False
    assert item.message_en == "Properties that are not allowed here: b, c."


def test_false_schema_and_big_values_are_summarized() -> None:
    from lae.validation.schema import _diagnostics

    schema: dict[str, Any] = {"properties": {"never": False, "text": {"maxLength": 3}}}
    items = list(
        _diagnostics(Draft7Validator(schema).iter_errors({"never": [1, 2], "text": "x" * 500}))
    )
    by_code = {item.code: item for item in items}
    assert by_code["LAE-SCHEMA-false"].params["instance_summary"] == {"length": 2}
    assert len(by_code["LAE-SCHEMA-maxLength"].params["instance_summary"]) == 120


@pytest.mark.parametrize(
    "value",
    [
        "-0500-01-01T00:00:00Z",
        "0000-06-01T00:00:00Z",
        "1642-01-01T00:00:00Z",
        "1642-01-01T00:00:00",
    ],
)
def test_date_time_format_accepts_signed_years(value: str) -> None:
    record = base("HumanMadeObject", ["/produced_by/timespan/begin_of_the_begin", value])
    assert validate_schema(record) == []


@pytest.mark.parametrize("value", ["1642", "1642-01-01", "1642-02-30T00:00:00Z", "yesterday"])
def test_date_time_format_rejects_what_is_not_a_datetime(value: str) -> None:
    record = base("HumanMadeObject", ["/produced_by/timespan/end_of_the_end", value])
    items = validate_schema(record)
    formats = [item for item in items if item.code == "LAE-SCHEMA-format"]
    causes = [item.params.get("cause") for item in items]
    where = "/produced_by/timespan/end_of_the_end"
    assert any(item.path == where for item in formats) or (
        {"code": "LAE-SCHEMA-format", "path": where} in causes
    ), found(items)


def test_uri_format_is_what_expansion_accepts() -> None:
    record = base("HumanMadeObject", ["/id", "not an iri"])
    items = validate_schema(record)
    assert ("LAE-SCHEMA-format", "violation", "/id") in found(items)
    assert next(i for i in items if i.code == "LAE-SCHEMA-format").params["format"] == "uri"


def test_provenance_activity_uses_the_provenance_schema() -> None:
    assert validator_for_record(PROVENANCE).schema["$id"] == SCHEMA_BASE_URI + "provenance.json"
    assert schema_for_type("Activity").schema["$id"] == SCHEMA_BASE_URI + "event.json"
    plain = {key: value for key, value in PROVENANCE.items() if key != "classified_as"}
    assert validator_for_record(plain).schema["$id"] == SCHEMA_BASE_URI + "event.json"


@pytest.mark.parametrize("record_type", sorted(SCHEMA_BY_TYPE))
def test_schema_for_type(record_type: str) -> None:
    validator = schema_for_type(record_type)
    assert validator.schema["$id"] == SCHEMA_BASE_URI + SCHEMA_BY_TYPE[record_type]
    assert validator is schema_for_type(record_type), "validators are built once"


def test_unresolvable_ref_is_a_diagnostic_not_an_exception() -> None:
    record = load_record(CORPUS_DIR / "linkedart" / "provenance-3.json")
    items = validate_schema(record)
    refs = [item for item in items if item.code == "LAE-SCHEMA-ref"]
    assert len(refs) == 1
    assert refs[0].severity == "violation" and refs[0].path == ""
    assert refs[0].params["ref"].endswith("LegalRight")
    assert "LegalRight" in refs[0].message_en


@pytest.mark.parametrize(
    ("document", "path", "type_param"),
    [
        ({"type": "Nonsense", "id": "https://example.org/x"}, "/type", "Nonsense"),
        ({"type": "Name", "content": "an embedded class"}, "/type", "Name"),
        ({"id": "https://example.org/x"}, "", None),
        ({"type": ["HumanMadeObject"]}, "/type", None),
        ([], "", None),
        ("text", "", None),
        (None, "", None),
    ],
)
def test_not_a_record(document: Any, path: str, type_param: str | None) -> None:
    item = only(validate_schema(document))
    assert (item.code, item.path, item.severity) == ("LAE-SCHEMA-recordType", path, "violation")
    assert item.params == {"type": type_param}


def test_corpus_schema_failures_are_exactly_the_known_ones() -> None:
    failing = set()
    for path in corpus_files():
        items = validate_schema(load_record(path))
        assert all(item.code in schema_codes() for item in items), found(items)
        if items:
            failing.add(path.relative_to(CORPUS_DIR).as_posix())
    assert failing == KNOWN_FAILURES
    assert len(failing) == 60


def test_every_keyword_the_vendored_schemas_use_is_known() -> None:
    """A keyword an error could name must have a code (and so a message)."""
    passing_on = {
        "$ref",
        "allOf",
        "if",
        "then",
        "else",
        "items",
        "properties",
        "patternProperties",
        "propertyNames",
    }
    used: set[str] = set()

    def collect(schema: Any) -> None:
        if isinstance(schema, dict):
            used.update(key for key in schema if key in Draft7Validator.VALIDATORS)
            for value in schema.values():
                collect(value)
        elif isinstance(schema, list):
            for value in schema:
                collect(value)

    for path in SCHEMA_DIR.glob("*.json"):
        collect(json.loads(path.read_bytes()))
    assert used - passing_on <= set(REPORTED_KEYWORDS)
    every_keyword = set(REPORTED_KEYWORDS) | (passing_on - {"then", "else"})
    assert set(Draft7Validator.VALIDATORS) == every_keyword
