"""The pipeline: schema, then rules and SHACL; database facts; the corpus; batches."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lae.corpus import CORPUS_DIR, corpus_files, load_record
from lae.db.models import EntityLabel, Record, RecordStatus
from lae.rdf import (
    BlankNodeError,
    ExpansionError,
    InvalidIRIError,
    InvalidLiteralError,
    OwnershipError,
)
from lae.validation import (
    Diagnostics,
    ReferenceFacts,
    engine,
    expansion,
    load_reference_facts,
    validate,
    validate_document,
    validate_many,
)
from lae.validation.expansion import RDF_CODES, expansion_diagnostic
from tests.validation.conftest import base, found, with_node_iris

MATERIAL = "http://vocab.getty.edu/aat/300015050"
PERSON = "https://example.org/person/1"
KNOWN_FAILURES = set(
    re.findall(
        r"^\| `([^`]+\.json)` \|",
        (CORPUS_DIR / "KNOWN_FAILURES.md").read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
)


NOT_A_MATERIAL = ReferenceFacts({MATERIAL: "Place"})
"""The schema already pins the declared ``type`` of a ``made_of`` reference, so a record that
passes it can only fail ``LAE-SHACL-006`` on what ``entity_labels`` says about the target."""


def codes(result: Diagnostics) -> list[str]:
    return [item.code for item in result.items]


# --- pipeline -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["HumanMadeObject", "Person", "Place", "Type"])
def test_valid_record(name: str) -> None:
    result = validate_document(base(name))
    assert result.items == [] and result.ok


def test_with_or_without_internal_node_iris_the_result_is_the_same() -> None:
    doc = base(
        "HumanMadeObject",
        ["/identified_by/1/content", " "],
        ["/produced_by/timespan/end_of_the_end", None],
    )
    plain = validate_document(doc, None, NOT_A_MATERIAL)
    assert found(plain.items) == [
        ("LAE-RULE-003", "violation", "/identified_by/1/content"),
        ("LAE-SHACL-006", "violation", "/made_of/0"),
        ("LAE-SHACL-014", "warning", "/produced_by/timespan"),
    ]
    assert validate_document(with_node_iris(doc), None, NOT_A_MATERIAL) == plain
    assert not plain.ok


def test_input_is_not_modified() -> None:
    doc = base()
    before = json.dumps(doc, sort_keys=True)
    validate_document(doc)
    assert json.dumps(doc, sort_keys=True) == before


def test_schema_violations_stop_the_pipeline() -> None:
    """The record also breaks a rule and a shape, but only the schema speaks."""
    doc = base("HumanMadeObject", ["/identified_by/1/content", " "])
    assert codes(validate_document(doc, None, NOT_A_MATERIAL)) == ["LAE-RULE-003", "LAE-SHACL-006"]
    del doc["_label"]
    result = validate_document(doc, None, NOT_A_MATERIAL)
    assert codes(result) == ["LAE-SCHEMA-required"]
    assert not result.ok


def test_schema_already_pins_the_declared_type_of_a_reference() -> None:
    doc = base("HumanMadeObject", ["/made_of/0/type", "Type"])
    assert found(validate_document(doc).items) == [
        ("LAE-SCHEMA-const", "violation", "/made_of/0/type")
    ]


def test_warnings_only_is_ok() -> None:
    result = validate_document(base("HumanMadeObject", ["/identified_by", None]))
    assert found(result.items) == [("LAE-SHACL-001", "warning", "/identified_by")]
    assert result.ok


def test_violations_come_first_then_by_path() -> None:
    doc = base(
        "HumanMadeObject",
        ["/identified_by", None],
        ["/produced_by/timespan/begin_of_the_begin", "1700-01-01T00:00:00Z"],
    )
    result = validate_document(doc, None, NOT_A_MATERIAL)
    assert [(item.severity, item.code) for item in result.items] == [
        ("violation", "LAE-SHACL-006"),
        ("violation", "LAE-RULE-001"),
        ("warning", "LAE-SHACL-001"),
    ]


def test_bce_timespan_is_valid_end_to_end() -> None:
    doc = base(
        "HumanMadeObject",
        ["/produced_by/timespan/begin_of_the_begin", "-0500-01-01T00:00:00Z"],
        ["/produced_by/timespan/end_of_the_end", "-0400-12-31T23:59:59Z"],
    )
    assert validate_document(doc).items == []
    doc["produced_by"]["timespan"]["end_of_the_end"] = "-0600-12-31T23:59:59Z"
    assert found(validate_document(doc).items) == [
        ("LAE-RULE-001", "violation", "/produced_by/timespan")
    ]


def test_record_uri_defaults_to_the_root_id() -> None:
    doc = base("HumanMadeObject", ["/identified_by", None])
    assert validate_document(doc, doc["id"]) == validate_document(doc)
    assert validate_document(doc).items != []


def test_root_id_that_is_not_the_record_uri_is_an_ownership_violation() -> None:
    result = validate_document(base(), "https://example.org/object/another")
    assert found(result.items) == [("LAE-RDF-002", "violation", "/id")]


def test_not_a_record() -> None:
    assert codes(validate_document([])) == ["LAE-SCHEMA-recordType"]
    assert codes(validate_document({"type": "Name"})) == ["LAE-SCHEMA-recordType"]


# --- expansion errors -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (BlankNodeError("b"), "LAE-RDF-001"),
        (OwnershipError("o"), "LAE-RDF-002"),
        (InvalidIRIError("i"), "LAE-RDF-003"),
        (InvalidLiteralError("l"), "LAE-RDF-004"),
        (ExpansionError("e"), "LAE-RDF-005"),
    ],
)
def test_each_expansion_error_has_its_own_code(error: ExpansionError, code: str) -> None:
    item = expansion_diagnostic(error)
    assert (item.code, item.severity, item.path) == (code, "violation", "")
    assert item.params == {"detail": str(error)}
    assert code in RDF_CODES and len(set(RDF_CODES)) == 6  # with the warning LAE-RDF-006


def test_an_expansion_error_whose_cause_cannot_be_found_is_at_the_root() -> None:
    """``InvalidLiteralError`` for a reason other than bad Unicode or a non-finite number."""
    item = expansion_diagnostic(InvalidLiteralError("l"), base(), base()["id"])
    assert item.path == ""
    assert expansion_diagnostic(OwnershipError("o"), base(), base()["id"]).path == ""
    assert expansion_diagnostic(OwnershipError("o"), [1], None).path == ""


def test_non_ascii_digits_in_a_date_are_a_schema_violation_with_a_path() -> None:
    """``lae.rdf.sanitize.VALID_DATETIME`` would let them through to the N-Triples."""
    doc = base(
        "HumanMadeObject",
        ["/produced_by/timespan/begin_of_the_begin", "\u0661\u0666\u0664\u0662-01-01T00:00:00Z"],
    )
    result = validate_document(doc)
    assert not result.ok
    assert {(item.code, item.path) for item in result.items} == {
        ("LAE-SCHEMA-format", "/produced_by/timespan/begin_of_the_begin")
    }


def test_every_expansion_error_subclass_is_mapped() -> None:
    def subclasses(cls: type) -> set[type]:
        return {cls}.union(*(subclasses(sub) for sub in cls.__subclasses__()))

    class Unmapped(InvalidIRIError):
        pass

    assert expansion_diagnostic(Unmapped("x")).code == "LAE-RDF-003"
    mapped = {cls for cls, _ in expansion._CODE_BY_ERROR}
    assert subclasses(ExpansionError) - {Unmapped} == mapped


def test_schema_valid_records_that_cannot_be_expanded_are_violations_not_exceptions() -> None:
    """What the schema lets through and expansion refuses (docs/questions.md, Step 1.3, item 3)."""
    nested_context = base()
    nested_context["produced_by"]["@context"] = {"content": "http://example.org/evil"}
    assert found(validate_document(nested_context).items) == [("LAE-RDF-005", "violation", "")]

    # A literal RDF cannot hold is found again in the document, so the editor can focus it.
    lone_surrogate = base("HumanMadeObject", ["/identified_by/0/content", "bad \ud800 text"])
    assert found(validate_document(lone_surrogate).items) == [
        ("LAE-RDF-004", "violation", "/identified_by/0/content")
    ]

    overflow = base("HumanMadeObject", ["/dimension/0/value", float("inf")])
    assert found(validate_document(overflow).items) == [
        ("LAE-RDF-004", "violation", "/dimension/0/value")
    ]

    assert found(validate_document(base(), "not an iri").items) == [
        ("LAE-RDF-003", "violation", "")
    ]


def test_expansion_failure_keeps_the_rule_diagnostics() -> None:
    doc = base("HumanMadeObject", ["/identified_by/1/content", " "])
    doc["produced_by"]["@context"] = {"content": "http://example.org/evil"}
    result = validate_document(doc)
    assert found(result.items) == [
        ("LAE-RDF-005", "violation", ""),
        ("LAE-RULE-003", "violation", "/identified_by/1/content"),
    ]
    assert not result.ok


def test_a_malformed_iri_is_located_by_the_schema_before_expansion_sees_it() -> None:
    doc = base()
    doc["identified_by"][0]["language"][0]["id"] = "relative/iri"
    (item,) = validate_document(doc).items
    assert item.params["cause"] == {
        "code": "LAE-SCHEMA-format",
        "path": "/identified_by/0/language/0/id",
    }


# --- corpus ---------------------------------------------------------------------------------


def test_corpus_has_no_violation_beyond_the_known_schema_failures() -> None:
    """Step 1.9 Verify: SHACL and the rules report zero violations on schema-valid records."""
    schema_failures: set[str] = set()
    violations: dict[str, list[tuple[str, str, str]]] = {}
    warnings: dict[str, int] = {}
    for path in corpus_files():
        relative = path.relative_to(CORPUS_DIR).as_posix()
        result = validate_document(load_record(path))
        if any(item.code.startswith("LAE-SCHEMA-") for item in result.items):
            assert all(item.code.startswith("LAE-SCHEMA-") for item in result.items)
            schema_failures.add(relative)
            continue
        bad = [item for item in result.items if item.severity == "violation"]
        if bad:
            violations[relative] = found(bad)
        assert result.ok == (not bad)
        for item in result.items:
            warnings[item.code] = warnings.get(item.code, 0) + 1
    assert violations == {}
    assert schema_failures == KNOWN_FAILURES
    # docs/spikes/S4-shacl.md, Appendix C lists these; update both together.
    assert warnings == {
        "LAE-SHACL-001": 46,
        "LAE-SHACL-002": 7,
        "LAE-SHACL-014": 1,
        "LAE-SHACL-015": 18,
        "LAE-SHACL-018": 2,
        "LAE-SHACL-020": 3,
    }


# --- database facts -------------------------------------------------------------------------


async def test_validate_without_a_session_takes_references_at_their_word() -> None:
    result = await validate(base(), None, None)
    assert result.items == []


async def test_validate_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    original = engine.validate_document

    def spy(*args: Any, **kwargs: Any) -> Diagnostics:
        seen.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "validate_document", spy)
    await validate(base())
    assert seen and seen[0] != threading.get_ident()


async def test_referenced_types_come_from_entity_labels(db_session: AsyncSession) -> None:
    db_session.add(EntityLabel(uri=MATERIAL, type="Place", default_label="oil paint"))
    await db_session.flush()
    facts = await load_reference_facts(db_session, {MATERIAL, PERSON})
    assert facts == ReferenceFacts({MATERIAL: "Place"}, frozenset())
    result = await validate(base(), None, db_session)
    assert found(result.items) == [("LAE-SHACL-006", "violation", "/made_of/0")]


async def test_aat_concepts_seeded_as_type_keep_their_declared_subclass(
    db_session: AsyncSession,
) -> None:
    """``lae bootstrap aat-labels`` stores every AAT concept as ``Type``. A record that uses one
    as a Material, a Language or a MeasurementUnit must still be publishable."""
    doc = base()
    references = [
        doc["made_of"][0],
        doc["identified_by"][0]["language"][0],
        doc["dimension"][0]["unit"],
    ]
    assert [reference["type"] for reference in references] == [
        "Material",
        "Language",
        "MeasurementUnit",
    ]
    for reference in references:
        db_session.add(EntityLabel(uri=reference["id"], type="Type", default_label="x"))
    await db_session.flush()
    result = await validate(doc, None, db_session)
    assert result.items == []


async def test_reference_to_a_deleted_record(db_session: AsyncSession) -> None:
    for uri, status in ((PERSON, RecordStatus.DELETED), (MATERIAL, RecordStatus.MERGED)):
        record = Record(uri=uri, type="Person", etag="e", data={}, context_version=1)
        record.status = status
        db_session.add(record)
    await db_session.flush()
    result = await validate(base(), None, db_session)
    assert found(result.items) == [
        ("LAE-RULE-004", "violation", "/produced_by/part/0/carried_out_by/0")
    ]
    assert result.items[0].params == {"uri": PERSON}


async def test_no_references_no_queries(db_session: AsyncSession) -> None:
    assert await load_reference_facts(db_session, set()) == ReferenceFacts()


# --- batches --------------------------------------------------------------------------------


def batch() -> list[tuple[dict[str, Any], str | None]]:
    good = base()
    warned = base("Person", ["/identified_by", None])
    broken = base("HumanMadeObject", ["/identified_by/0/content", ""])
    not_schema_valid = base("Place", ["/_label", None])
    return [(doc, None) for doc in (good, warned, broken, not_schema_valid)]


async def test_validate_many_small_batch_keeps_order() -> None:
    results = await validate_many(batch())
    assert [codes(result) for result in results] == [
        [],
        ["LAE-SHACL-015"],
        ["LAE-RULE-003"],
        ["LAE-SCHEMA-required"],
    ]
    assert [result.ok for result in results] == [True, True, False, False]


async def test_validate_many_uses_a_process_pool_for_large_batches() -> None:
    documents = batch() * 6
    assert len(documents) >= engine.POOL_THRESHOLD
    pooled = await asyncio.wait_for(validate_many(documents, max_workers=2), timeout=120)
    assert pooled == [validate_document(doc, uri) for doc, uri in documents]


async def test_validate_many_reads_facts_once_for_the_batch(db_session: AsyncSession) -> None:
    db_session.add(EntityLabel(uri=MATERIAL, type="Place", default_label="oil paint"))
    record = Record(uri=PERSON, type="Person", etag="e", data={}, context_version=1)
    record.status = RecordStatus.DELETED
    db_session.add(record)
    await db_session.flush()
    results = await validate_many([(base(), None), (base("Person"), None)], db_session)
    assert found(results[0].items) == [
        ("LAE-RULE-004", "violation", "/produced_by/part/0/carried_out_by/0"),
        ("LAE-SHACL-006", "violation", "/made_of/0"),
    ]
    assert results[1].items == []


def test_jobs_carry_only_their_own_facts() -> None:
    facts = ReferenceFacts(
        {MATERIAL: "Type", "https://example.org/other": "Place"}, frozenset({PERSON, "x"})
    )
    _, _, own, version = engine._job(base(), None, facts, 1)
    assert own == ReferenceFacts({MATERIAL: "Type"}, frozenset({PERSON}))
    assert version == 1


def test_warm_up_loads_every_schema_and_the_shapes() -> None:
    engine.warm_up()
    assert engine.schema_validator.cache_info().currsize == 12  # one per schema file in use
    assert len(engine.shapes_graph()) > 100
