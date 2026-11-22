"""The type -> schema mapping and the offline registry over ``profiles/schema/``."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from referencing import Registry
from referencing.exceptions import Unresolvable

from lae.validation.schema_map import (
    PROVENANCE_ACTIVITY,
    PROVENANCE_SCHEMA,
    SCHEMA_BASE_URI,
    SCHEMA_BY_TYPE,
    load_registry,
    schema_files,
    schema_name_for,
    validator_for,
)

# Upstream defect, recorded in profiles/schema/SOURCE.md: provenance.json refers to a definition
# that core.json does not have. The exact set is asserted so a fix or a new break is noticed.
KNOWN_DANGLING_REFS = {("provenance.json", "core.json#/$defs/LegalRight")}


def refs(node: Any) -> Iterator[str]:
    """Every ``$ref`` value in a schema document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from refs(item)


def declared_types(schema: dict[str, Any]) -> list[str]:
    """The record types an entry-point schema accepts (its specific ``type`` constraint)."""
    specific = schema["properties"]["type"]["allOf"][1]
    return [specific["const"]] if "const" in specific else list(specific["enum"])


@pytest.fixture(scope="module")
def registry() -> Registry[Any]:
    return load_registry()


def test_every_ref_resolves_offline(registry: Registry[Any]) -> None:
    dangling = set()
    for path, schema in schema_files():
        resolver = registry.resolver(base_uri=SCHEMA_BASE_URI + path.name)
        for ref in refs(schema):
            try:
                resolver.lookup(ref)
            except Unresolvable:
                dangling.add((path.name, ref))
    assert dangling == KNOWN_DANGLING_REFS


def test_mapping_matches_the_schemas_type_constraints() -> None:
    entry_points = {
        path.name: declared_types(schema)
        for path, schema in schema_files()
        if "type" in schema.get("properties", {})
    }
    assert set(entry_points) == set(SCHEMA_BY_TYPE.values()) | {PROVENANCE_SCHEMA}
    assert entry_points[PROVENANCE_SCHEMA] == ["Activity"]
    for name, types in entry_points.items():
        if name != PROVENANCE_SCHEMA:
            assert {t: SCHEMA_BY_TYPE.get(t) for t in types} == dict.fromkeys(types, name)
    for record_type, name in SCHEMA_BY_TYPE.items():
        assert record_type in entry_points[name]


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"type": "HumanMadeObject"}, "object.json"),
        ({"type": "Type"}, "concept.json"),
        ({"type": "Material"}, "concept.json"),
        ({"type": "VisualItem"}, "image.json"),
        ({"type": "LinguisticObject"}, "text.json"),
        ({"type": "PropositionalObject"}, "abstract.json"),
        ({"type": "Period"}, "event.json"),
        ({"type": "Activity"}, "event.json"),
        (
            {"type": "Activity", "classified_as": [{"id": "http://vocab.getty.edu/aat/300054766"}]},
            "event.json",
        ),
        ({"type": "Activity", "classified_as": [{"id": PROVENANCE_ACTIVITY}]}, "provenance.json"),
        ({"type": "Event", "classified_as": [{"id": PROVENANCE_ACTIVITY}]}, "event.json"),
    ],
)
def test_schema_name_for(record: dict[str, Any], expected: str) -> None:
    assert schema_name_for(record) == expected


@pytest.mark.parametrize("record", [{}, {"type": "Name"}, {"type": ["Person"]}])
def test_schema_name_for_rejects_non_record_types(record: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="not a Linked Art record type"):
        schema_name_for(record)


def test_validator_reports_errors_from_referenced_files(registry: Registry[Any]) -> None:
    validator = validator_for("person.json", registry)
    record = {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": "https://example.org/person/1",
        "type": "Person",
        "_label": "Example",
        "identified_by": [{"type": "Name"}],
    }
    assert validator.is_valid({**record, "identified_by": [{"type": "Name", "content": "Ex"}]})
    assert not validator.is_valid(record)
