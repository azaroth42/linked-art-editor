"""The Python rules ``LAE-RULE-001`` to ``-005`` (``lae.validation.rules``)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from lae.validation.rules import EDTF_TYPE, RULE_CODES, validate_rules
from tests.validation.conftest import base, found

TIMESPAN = "/produced_by/timespan"
BEGIN = f"{TIMESPAN}/begin_of_the_begin"
END = f"{TIMESPAN}/end_of_the_end"


def edtf_name(content: str) -> dict[str, Any]:
    return {
        "type": "Name",
        "content": content,
        "classified_as": [{"id": EDTF_TYPE, "type": "Type", "_label": "EDTF"}],
    }


def test_valid_record_has_no_rule_diagnostics() -> None:
    for name in ("HumanMadeObject", "Person", "Place", "Type"):
        assert validate_rules(base(name)) == []


def test_codes_are_declared() -> None:
    assert RULE_CODES == tuple(f"LAE-RULE-00{n}" for n in range(1, 6))


# --- LAE-RULE-001 ---------------------------------------------------------------------------


def test_begin_after_end_is_a_violation_on_the_timespan() -> None:
    record = base("HumanMadeObject", [BEGIN, "1643-01-01T00:00:00Z"])
    (item,) = validate_rules(record)
    assert (item.code, item.severity, item.path) == ("LAE-RULE-001", "violation", TIMESPAN)
    assert item.params == {"begin": "1643-01-01T00:00:00Z", "end": "1642-12-31T23:59:59Z"}
    assert "1643-01-01T00:00:00Z" in item.message_en


@pytest.mark.parametrize(
    ("begin", "end", "in_order"),
    [
        ("1642-01-01T00:00:00Z", "1642-01-01T00:00:00Z", True),
        ("-0500-01-01T00:00:00Z", "-0400-12-31T23:59:59Z", True),
        ("-0400-01-01T00:00:00Z", "-0500-12-31T23:59:59Z", False),
        ("-0044-03-15T00:00:00Z", "0014-08-19T00:00:00Z", True),
        ("0014-08-19T00:00:00Z", "-0044-03-15T00:00:00Z", False),
        ("-12000-01-01T00:00:00Z", "-9000-01-01T00:00:00Z", True),
        ("-9000-01-01T00:00:00Z", "-12000-01-01T00:00:00Z", False),
        ("1642-01-01T00:00:00+02:00", "1641-12-31T23:00:00Z", True),
        ("1642-01-01T00:00:00-02:00", "1642-01-01T01:00:00Z", False),
        ("1642-06-01T00:00:00.5Z", "1642-06-01T00:00:00.25Z", False),
        ("9999-12-31T23:59:59Z", "10000-01-01T00:00:00Z", True),
    ],
)
def test_ordering_uses_signed_years_and_instants(begin: str, end: str, in_order: bool) -> None:
    record = base("HumanMadeObject", [BEGIN, begin], [END, end])
    codes = [item.code for item in validate_rules(record)]
    assert codes == ([] if in_order else ["LAE-RULE-001"])


def test_only_the_outer_bounds_are_compared() -> None:
    record = base(
        "HumanMadeObject",
        [f"{TIMESPAN}/end_of_the_begin", "1700-01-01T00:00:00Z"],
        [f"{TIMESPAN}/begin_of_the_end", "1600-01-01T00:00:00Z"],
    )
    assert validate_rules(record) == []


def test_a_timespan_with_one_bound_is_not_compared() -> None:
    record = base("HumanMadeObject", [END, None])
    assert validate_rules(record) == []


def test_every_timespan_in_the_record_is_checked() -> None:
    record = base("Person", ["/died/timespan/begin_of_the_begin", "1700-01-01T00:00:00Z"])
    assert found(validate_rules(record)) == [("LAE-RULE-001", "violation", "/died/timespan")]


# --- LAE-RULE-005 ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1642", "1642-01-01", "1642-02-30T00:00:00Z", "", 1642, None])
def test_invalid_bound_is_a_violation_on_the_bound(value: Any) -> None:
    record = base()
    record["produced_by"]["timespan"]["end_of_the_begin"] = value
    (item,) = validate_rules(record)
    assert (item.code, item.severity) == ("LAE-RULE-005", "violation")
    assert item.path == f"{TIMESPAN}/end_of_the_begin"
    assert item.params == {"property": "end_of_the_begin", "value": value}


def test_an_invalid_bound_is_reported_once_and_not_compared() -> None:
    record = base("HumanMadeObject", [BEGIN, "1700"])
    assert found(validate_rules(record)) == [("LAE-RULE-005", "violation", BEGIN)]


# --- LAE-RULE-002 ---------------------------------------------------------------------------


def test_invalid_edtf_is_a_warning_on_the_content() -> None:
    record = base()
    record["produced_by"]["timespan"]["identified_by"] = [
        {"type": "Name", "content": "ca. 1642"},
        edtf_name("1642~"),
        edtf_name("1880~/1895??"),
    ]
    (item,) = validate_rules(record)
    assert (item.code, item.severity) == ("LAE-RULE-002", "warning")
    assert item.path == f"{TIMESPAN}/identified_by/2/content"
    assert item.params == {"value": "1880~/1895??"}


def test_display_names_of_a_timespan_are_free_text() -> None:
    record = base()
    record["produced_by"]["timespan"]["identified_by"] = [
        {"type": "Name", "content": "1503-1506"},
        {"type": "Name", "content": "between 1915 and 1916"},
    ]
    assert validate_rules(record) == []


def test_edtf_is_checked_only_in_a_timespan() -> None:
    record = base()
    record["identified_by"].append(edtf_name("not a date"))
    assert validate_rules(record) == []


# --- LAE-RULE-003 ---------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["", " ", "\n\t ", " "])
def test_empty_content_is_a_violation(content: str) -> None:
    record = base("HumanMadeObject", ["/identified_by/1/content", content])
    (item,) = validate_rules(record)
    assert (item.code, item.severity, item.path) == (
        "LAE-RULE-003",
        "violation",
        "/identified_by/1/content",
    )


def test_empty_content_anywhere() -> None:
    record = base()
    record["referred_to_by"] = [{"type": "LinguisticObject", "content": ""}]
    record["produced_by"]["timespan"]["identified_by"] = [edtf_name(" ")]
    assert found(validate_rules(record)) == [
        ("LAE-RULE-003", "violation", f"{TIMESPAN}/identified_by/0/content"),
        ("LAE-RULE-003", "violation", "/referred_to_by/0/content"),
    ]


def test_missing_or_non_string_content_is_left_to_the_schema() -> None:
    record = base("HumanMadeObject", ["/identified_by/1/content", None])
    record["identified_by"][0]["content"] = 5
    assert validate_rules(record) == []


# --- LAE-RULE-004 ---------------------------------------------------------------------------


def test_reference_to_a_deleted_record() -> None:
    record = base()
    deleted = {"https://example.org/person/1", "https://example.org/unrelated"}
    (item,) = validate_rules(record, deleted)
    assert (item.code, item.severity) == ("LAE-RULE-004", "violation")
    assert item.path == "/produced_by/part/0/carried_out_by/0"
    assert item.params == {"uri": "https://example.org/person/1"}


def test_every_occurrence_of_a_deleted_reference_is_reported() -> None:
    record = base()
    record["current_owner"] = [copy.deepcopy(record["produced_by"]["part"][0]["carried_out_by"][0])]
    paths = [item.path for item in validate_rules(record, {"https://example.org/person/1"})]
    assert paths == ["/produced_by/part/0/carried_out_by/0", "/current_owner/0"]


def test_the_record_itself_is_not_a_reference() -> None:
    record = base()
    assert validate_rules(record, {record["id"]}) == []


def test_context_is_never_walked() -> None:
    record = base()
    record["@context"] = {"content": "", "id": "https://example.org/person/1", "type": "TimeSpan"}
    items = validate_rules(record, {"https://example.org/person/1"})
    assert [item.path for item in items] == ["/produced_by/part/0/carried_out_by/0"]
