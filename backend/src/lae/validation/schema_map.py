"""Which vendored Linked Art JSON Schema validates which record, and an offline registry for them.

The schemas live in ``profiles/schema/`` (vendored from ``linked-art/json-validator``; see the
``SOURCE.md`` there). Each file declares a ``$id`` under ``SCHEMA_BASE_URI`` and refers to the
shared definitions as ``core.json#/$defs/...``, so a ``referencing`` registry keyed by ``$id``
resolves every ``$ref`` without the network. Step 1.9 (``lae.validation.schema``) builds
diagnostics on top of this module.

The mapping follows the ``type`` constraint of each entry-point schema. ``Activity`` is the one
type shared by two schemas: a provenance activity (``classified_as`` AAT 300055863, as the Linked
Art API's provenance endpoint requires) uses ``provenance.json``; every other ``Activity`` uses
``event.json``. That is why the lookup takes the whole record, not only its ``type``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from jsonschema import Draft7Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

SCHEMA_DIR: Final = Path(__file__).resolve().parents[4] / "profiles" / "schema"
SCHEMA_BASE_URI: Final = "https://linked.art/api/1.0/schema/"
PROVENANCE_ACTIVITY: Final = "http://vocab.getty.edu/aat/300055863"
PROVENANCE_SCHEMA: Final = "provenance.json"

SCHEMA_BY_TYPE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "HumanMadeObject": "object.json",
        "Person": "person.json",
        "Group": "group.json",
        "Place": "place.json",
        "Type": "concept.json",
        "Currency": "concept.json",
        "Language": "concept.json",
        "Material": "concept.json",
        "MeasurementUnit": "concept.json",
        "Set": "set.json",
        "DigitalObject": "digital.json",
        "LinguisticObject": "text.json",
        "VisualItem": "image.json",
        "PropositionalObject": "abstract.json",
        "Activity": "event.json",
        "Event": "event.json",
        "Period": "event.json",
    }
)
"""Top-level record ``type`` -> entry-point schema file (``Activity`` default; see module doc)."""

RECORD_TYPES: Final = frozenset(SCHEMA_BY_TYPE)
"""Every Linked Art class that can be the ``type`` of a top-level record."""


def is_provenance_activity(record: Mapping[str, Any]) -> bool:
    """Return True if the record is an ``Activity`` classified as a provenance activity."""
    classes = record.get("classified_as")
    return (
        record.get("type") == "Activity"
        and isinstance(classes, list)
        and any(isinstance(c, Mapping) and c.get("id") == PROVENANCE_ACTIVITY for c in classes)
    )


def schema_name_for(record: Mapping[str, Any]) -> str:
    """Return the entry-point schema file name (e.g. ``object.json``) for a record."""
    record_type = record.get("type")
    if not isinstance(record_type, str) or record_type not in SCHEMA_BY_TYPE:
        raise ValueError(f"not a Linked Art record type: {record_type!r}")
    if is_provenance_activity(record):
        return PROVENANCE_SCHEMA
    return SCHEMA_BY_TYPE[record_type]


def schema_files(schema_dir: Path = SCHEMA_DIR) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Yield ``(path, schema)`` for every vendored schema file, sorted by name."""
    for path in sorted(schema_dir.glob("*.json")):
        yield path, json.loads(path.read_bytes())


def load_registry(schema_dir: Path = SCHEMA_DIR) -> Registry[Any]:
    """Build a registry of every vendored schema keyed by its ``$id``; it never fetches."""
    resources: list[tuple[str, Resource[Any]]] = []
    for path, contents in schema_files(schema_dir):
        uri = contents.get("$id")
        if uri != SCHEMA_BASE_URI + path.name:
            raise ValueError(f"{path.name}: $id {uri!r} is not {SCHEMA_BASE_URI}{path.name}")
        # The files declare 2020-12 but are validated as Draft 7 (see profiles/schema/SOURCE.md).
        resources.append((uri, DRAFT7.create_resource(contents)))
    return Registry().with_resources(resources).crawl()


def validator_for(schema_name: str, registry: Registry[Any]) -> Draft7Validator:
    """Return a Draft 7 validator for one entry-point schema, resolving refs from ``registry``."""
    resource = registry[SCHEMA_BASE_URI + schema_name]
    return Draft7Validator(resource.contents, registry=registry)
