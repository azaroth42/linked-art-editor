"""Tests for ``lae.bootstrap.sanitize.clean_ids``."""

from __future__ import annotations

import copy
from typing import Any

from lae.bootstrap.sanitize import clean_ids
from lae.rdf import expand_to_ntriples

RECORD = "https://example.org/object/1"


def test_whitespace_in_ids_is_percent_encoded_and_line_breaks_removed() -> None:
    doc: dict[str, Any] = {
        "id": RECORD,
        "type": "HumanMadeObject",
        "classified_as": [{"id": "http://vocab.getty.edu/aat/3000\n33618", "type": "Type"}],
        "member_of": [{"id": "https://example.org/set/a b", "type": "Set"}],
    }
    before = copy.deepcopy(doc)
    cleaned = clean_ids(doc)
    assert doc == before
    assert cleaned.document["classified_as"][0]["id"] == "http://vocab.getty.edu/aat/300033618"
    assert cleaned.document["member_of"][0]["id"] == "https://example.org/set/a%20b"
    assert cleaned.repaired == [
        "http://vocab.getty.edu/aat/3000\n33618",
        "https://example.org/set/a b",
    ]
    assert cleaned.invalid == []


def test_unrepairable_ids_are_listed_and_left_alone() -> None:
    doc = {
        "id": RECORD,
        "type": "HumanMadeObject",
        "member_of": [{"id": "https:/no-host/set", "type": "Set"}, {"id": "_:b0", "type": "Set"}],
    }
    cleaned = clean_ids(doc)
    assert cleaned.invalid == ["https:/no-host/set", "_:b0"]
    assert cleaned.document == doc


def test_context_and_non_string_ids_are_not_touched() -> None:
    doc = {
        "@context": {"x": {"@id": "http://example.org/a b"}},
        "id": RECORD,
        "type": "HumanMadeObject",
        "@id": None,
    }
    cleaned = clean_ids(doc)
    assert cleaned.document == doc
    assert cleaned.repaired == cleaned.invalid == []


def test_a_repaired_document_expands() -> None:
    """pyld would drop the IRI with the space; expansion rejects it; the repaired one expands."""
    doc = {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": RECORD,
        "type": "HumanMadeObject",
        "member_of": [{"id": "https://example.org/set/a b", "type": "Set"}],
    }
    lines = expand_to_ntriples(clean_ids(doc).document, 1)
    assert any(line.endswith("<https://example.org/set/a%20b> .") for line in lines)
