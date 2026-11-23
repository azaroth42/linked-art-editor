"""One input document → the rows ``lae bootstrap load`` writes (runs in the worker processes).

For each document, in order:

1. Parse it (orjson); it must be a JSON object with a string root ``id`` (the record URI).
2. Repair ids (``lae.bootstrap.sanitize.clean_ids``); a record with an id that cannot be repaired
   is reported (``LAE-BOOT-002`` for the root, ``LAE-BOOT-003`` below it).
3. Validate its public form against the JSON Schema for its ``type``
   (``lae.validation.schema_map``): the document without internal node IRIs
   (``strip_internal_iris``), as ``/id/{uuid}`` would serve it, and without the ids that
   embedded nodes brought with them (``lae.rdf.strip_embedded_ids``, ADR-0028), as
   ``lae.validation.validate_schema`` checks it. The schemas forbid ``id`` on
   many embedded nodes (129 of the 184 valid corpus records fail with node IRIs added), and an
   input may already carry node IRIs (an export of this system, the Spike S6 generator).
   JSON Schema only, no SHACL.
4. Assign internal node IRIs (``lae.projection.assign_node_iris``, Spike S3 rule R4).
5. Build the ``records`` row: ``id`` is the UUID of a local record URI (``<base_uri><uuid>``) or a
   new UUIDv7 for a record that keeps a URI from elsewhere (ADR-0024), ``etag`` is
   ``lae.api.records.record_etag`` (BLAKE2b-128 of the canonical JSON, the same function publish
   uses), and the ``entity_labels`` row comes from ``lae.labels``.

References keep any ``_label`` the source gave them: write-time stub normalization
(``lae.labels.normalize``) needs every target's ``entity_labels`` row, which a single streaming
pass does not have, and transclusion on read replaces those labels anyway (ADR-0006).
"""

from __future__ import annotations

import functools
import time
from collections.abc import Iterable
from typing import Any, Final, NamedTuple
from uuid import UUID

import orjson
from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from lae.api.records import record_etag
from lae.bootstrap.common import (
    INVALID_ID,
    NO_RECORD_URI,
    NOT_A_RECORD,
    SCHEMA_INVALID,
    UNKNOWN_TYPE,
    Failure,
)
from lae.bootstrap.sanitize import clean_ids
from lae.bootstrap.source import SourceItem
from lae.ids import new_uuid7, record_id_from_uri
from lae.labels.extract import labels_from_record
from lae.labels.upsert import primary_classification
from lae.projection import assign_node_iris, canonical_json, strip_internal_iris
from lae.rdf import strip_embedded_ids
from lae.validation.schema_map import load_registry, schema_name_for, validator_for

STAGE: Final = "load"
MAX_ERRORS: Final = 10
"""JSON Schema errors listed per record in the report."""


class PreparedRecord(NamedTuple):
    """Everything ``COPY`` needs for one record and its ``entity_labels`` row."""

    id: UUID
    uri: str
    type: str
    etag: str
    data: str
    """Canonical JSON text of the stored document (internal node IRIs included)."""
    labels: str
    """JSON text of ``{language: label}``."""
    default_label: str
    primary_classification: str | None
    source: str


class PreparedBatch(NamedTuple):
    """What a worker returns for one batch of input items."""

    records: list[PreparedRecord]
    failures: list[Failure]
    repaired_ids: int
    cpu_s: float


@functools.cache
def _registry() -> Registry[Any]:
    return load_registry()


@functools.cache
def _validator(schema_name: str) -> Draft7Validator:
    return validator_for(schema_name, _registry())


def _summarize(error: ValidationError) -> str:
    pointer = "/" + "/".join(str(part) for part in error.absolute_path)
    if error.validator == "anyOf":
        detail = "matches none of the allowed shapes"
    elif error.validator == "type":
        detail = f"is not of type {error.validator_value!r}"
    else:
        detail = error.message
    return f"{pointer} {error.validator}: {detail[:300]}"


def schema_errors(record: dict[str, Any]) -> list[str]:
    """Return up to ``MAX_ERRORS`` sorted JSON Schema error summaries (empty if valid).

    Raises ``ValueError`` if the record's ``type`` has no schema (not a record class).
    """
    validator = _validator(schema_name_for(record))
    try:
        # One pass: as fast as is_valid for a valid record, and half the work for an invalid one.
        errors = sorted(_summarize(error) for error in validator.iter_errors(record))
    except Unresolvable as error:
        errors = [f"(schema) unresolvable $ref: {error.ref}"]
    if len(errors) > MAX_ERRORS:
        errors = [*errors[:MAX_ERRORS], f"... and {len(errors) - MAX_ERRORS} more"]
    return errors


def record_id_for(uri: str, base_uri: str) -> UUID:
    """The ``records.id`` of a record: its UUID if ``uri`` is local, else a new UUIDv7."""
    return record_id_from_uri(uri, base_uri=base_uri) or new_uuid7()


def prepare(item: SourceItem, base_uri: str) -> tuple[PreparedRecord | None, Failure | None, int]:
    """Turn one input item into a row, or a failure; also return the number of repaired ids."""
    try:
        document = orjson.loads(item.text)
    except orjson.JSONDecodeError as error:
        return None, Failure(STAGE, item.source, NOT_A_RECORD, None, [str(error)]), 0
    if not isinstance(document, dict):
        return None, Failure(STAGE, item.source, NOT_A_RECORD, None, ["not a JSON object"]), 0
    root_id = document.get("id")
    if not isinstance(root_id, str):
        return None, Failure(STAGE, item.source, NO_RECORD_URI, None, ["no root id"]), 0
    cleaned = clean_ids(document)
    if cleaned.invalid:
        code = NO_RECORD_URI if root_id in cleaned.invalid else INVALID_ID
        return None, Failure(STAGE, item.source, code, root_id, cleaned.invalid), 0
    document = cleaned.document
    uri: str = document["id"]
    try:
        errors = schema_errors(strip_internal_iris(strip_embedded_ids(document, uri), uri))
    except ValueError as error:
        return None, Failure(STAGE, item.source, UNKNOWN_TYPE, uri, [str(error)]), 0
    if errors:
        return None, Failure(STAGE, item.source, SCHEMA_INVALID, uri, errors), 0
    stored = assign_node_iris(document, uri)
    data = canonical_json(stored)
    labels, default_label = labels_from_record(stored)
    prepared = PreparedRecord(
        id=record_id_for(uri, base_uri),
        uri=uri,
        type=stored["type"],
        etag=record_etag(stored),
        data=data.decode("utf-8"),
        labels=orjson.dumps(labels).decode("utf-8"),
        default_label=default_label,
        primary_classification=primary_classification(stored),
        source=item.source,
    )
    return prepared, None, len(cleaned.repaired)


def prepare_batch(items: Iterable[SourceItem], base_uri: str) -> PreparedBatch:
    """Worker entry point: prepare a batch of input items."""
    cpu0 = time.process_time()
    records: list[PreparedRecord] = []
    failures: list[Failure] = []
    repaired = 0
    for item in items:
        record, failure, repaired_ids = prepare(item, base_uri)
        if record is not None:
            records.append(record)
        if failure is not None:
            failures.append(failure)
        repaired += repaired_ids
    return PreparedBatch(records, failures, repaired, time.process_time() - cpu0)
