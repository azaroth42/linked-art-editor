"""``Diagnostic`` / ``Diagnostics``, JSON Pointers, and message rendering."""

from __future__ import annotations

from lae.validation.diagnostics import (
    Diagnostics,
    child,
    diagnostic,
    message_en,
    ordered,
    pointer,
)


def test_ok_means_no_violation() -> None:
    warning = diagnostic("LAE-SHACL-001", "/identified_by", "warning")
    info = diagnostic("LAE-SHACL-001", "", "info")
    violation = diagnostic("LAE-RULE-003", "/identified_by/0/content", "violation")
    assert Diagnostics(items=[]).ok
    assert Diagnostics(items=[warning, info]).ok
    assert not Diagnostics(items=[warning, violation]).ok


def test_serialized_form_is_the_api_contract() -> None:
    item = diagnostic("LAE-RULE-004", "/made_of/0", "violation", {"uri": "https://example.org/x"})
    assert Diagnostics(items=[item]).model_dump() == {
        "items": [
            {
                "code": "LAE-RULE-004",
                "path": "/made_of/0",
                "severity": "violation",
                "params": {"uri": "https://example.org/x"},
                "message_en": "This refers to a record that has been deleted: https://example.org/x.",
            }
        ],
        "ok": False,
    }


def test_json_pointer_escaping() -> None:
    assert pointer([]) == ""
    assert pointer(["identified_by", 0, "content"]) == "/identified_by/0/content"
    assert pointer(["a/b", "c~d"]) == "/a~1b/c~0d"
    assert child("", "@context") == "/@context"
    assert child("/x", "~/") == "/x/~0~1"


def test_message_placeholders_are_filled_from_params() -> None:
    text = message_en("LAE-SCHEMA-required", {"missing": ["id", "_label"]})
    assert text == "Required properties are missing: id, _label."
    assert message_en("LAE-SCHEMA-maxItems", {"validator_value": 1}) == (
        "This list may have at most 1 items."
    )
    # A placeholder without a parameter stays visible instead of raising.
    assert "{missing}" in message_en("LAE-SCHEMA-required", {})


def test_ordered_is_deterministic_and_drops_exact_duplicates() -> None:
    first = diagnostic("LAE-SHACL-006", "/made_of/1", "violation", {"value": "b"})
    second = diagnostic("LAE-SHACL-006", "/made_of/0", "violation", {"value": "a"})
    warning = diagnostic("LAE-SHACL-001", "", "warning")
    info = diagnostic("LAE-SHACL-001", "", "info")
    assert ordered([info, warning, first, second, first]) == [second, first, warning, info]
