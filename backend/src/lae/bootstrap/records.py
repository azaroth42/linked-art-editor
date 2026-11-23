"""One input document → the rows ``lae bootstrap load`` writes (runs in the worker processes).

``parse`` reads an item and finds its source URI; ``build`` does the rest. Between the two the
loader drops the items whose source URI is already loaded, so a repeated load does none of the
expensive work. For each document, in order:

1. Parse it (orjson); it must be a JSON object with a string root ``id``, its *source URI*.
2. Repair ids (``lae.bootstrap.sanitize.clean_ids``); a record with an id that cannot be repaired
   is rejected (``LAE-BOOT-002`` for the root, ``LAE-BOOT-003`` below it).
3. Its root ``type`` must be a Linked Art record class, one with a JSON Schema
   (``lae.validation.schema_map``); else it is rejected (``LAE-BOOT-004``): the editor has no
   profile for it.
4. **Re-mint** (ADR-0030). A source URI of the form ``<base_uri><uuid>`` is kept and its UUID is
   the record id. Any other gets a new UUIDv7 id and the URI ``<base_uri><uuidv7>``;
   ``lae.bootstrap.remint.remint`` rewrites the document and adds the source URI to
   ``equivalent``, and the source URI becomes a ``record_aliases`` row.
5. Assign internal node IRIs (``lae.projection.assign_node_iris``, Spike S3 rule R4).
6. **Expand to RDF** with ``lae.rdf.expand_to_ntriples``, the production expansion. A record it
   refuses is rejected (``LAE-BOOT-007``) and never reaches PostgreSQL, so PostgreSQL and QLever
   cannot disagree about which records exist. The triples are thrown away; ``export-nt`` makes
   them again from the final document.
7. **Validate** against the JSON Schema for its type (``lae.validation.validate_schema``, which
   ignores internal node IRIs, the ids embedded nodes brought with them and nested repeats of
   the pinned ``@context``; ADR-0028). A record that fails is loaded all the same, and its
   violations become ``qa_findings`` rows (``lae.bootstrap.findings``).
8. Build the ``records`` row (``etag`` is ``lae.api.records.record_etag``, the function publish
   uses) and the ``entity_labels`` row (``lae.labels``).

References to other records are not touched here: a worker sees one batch, not the whole load.
``lae.bootstrap.rewrite`` rewrites them, and strips ``_label`` from references whose target is
known, in a second pass. Neither changes whether a record can be expanded (ADR-0030).
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any, Final, NamedTuple
from uuid import UUID

import orjson

from lae.api.records import record_etag
from lae.bootstrap.common import (
    EXPANSION_FAILED,
    INVALID_ID,
    NO_RECORD_URI,
    NOT_A_RECORD,
    PREPARE_FAILED,
    UNKNOWN_TYPE,
    Failure,
)
from lae.bootstrap.findings import schema_findings
from lae.bootstrap.remint import local_record_id, remint
from lae.bootstrap.sanitize import clean_ids
from lae.bootstrap.source import SourceItem
from lae.ids import new_uuid7, record_uri
from lae.labels.extract import labels_from_record
from lae.labels.upsert import primary_classification
from lae.projection import assign_node_iris, canonical_json
from lae.rdf import ExpansionError, expand_to_ntriples
from lae.validation.schema import validate_schema
from lae.validation.schema_map import schema_name_for

STAGE: Final = "load"


class ParsedItem(NamedTuple):
    """An input item that is a JSON object with a usable root ``id``."""

    source: str
    """Where it came from: ``<file>`` or ``<file>:<line>``."""
    source_uri: str
    """Its root ``id`` after repair: the URI the source knows it by."""
    document: dict[str, Any]
    repaired_ids: int


class PreparedRecord(NamedTuple):
    """Everything the loader writes for one record."""

    id: UUID
    uri: str
    """``<base_uri><id>``."""
    source_uri: str
    alias: str | None
    """``source_uri`` if the record was re-minted (its ``record_aliases`` row), else ``None``."""
    type: str
    etag: str
    data: str
    """Canonical JSON text of the stored document (internal node IRIs included)."""
    labels: str
    """JSON text of ``{language: label}``."""
    default_label: str
    primary_classification: str | None
    findings: list[tuple[str, str]]
    """``(code, params JSON)`` per ``qa_findings`` row (``lae.bootstrap.findings``)."""
    source: str


class PreparedBatch(NamedTuple):
    """What ``build_batch`` returns for one batch of parsed items."""

    records: list[PreparedRecord]
    failures: list[Failure]
    cpu_s: float


def parse(item: SourceItem) -> ParsedItem | Failure:
    """Read one input item: a record to build, or the reason it is not one."""
    try:
        document = orjson.loads(item.text)
    except orjson.JSONDecodeError as error:
        return Failure(STAGE, item.source, NOT_A_RECORD, None, [str(error)])
    if not isinstance(document, dict):
        return Failure(STAGE, item.source, NOT_A_RECORD, None, ["not a JSON object"])
    root_id = document.get("id")
    if not isinstance(root_id, str):
        return Failure(STAGE, item.source, NO_RECORD_URI, None, ["no root id"])
    cleaned = clean_ids(document)
    if cleaned.invalid:
        code = NO_RECORD_URI if root_id in cleaned.invalid else INVALID_ID
        return Failure(STAGE, item.source, code, root_id, cleaned.invalid)
    source_uri: str = cleaned.document["id"]
    return ParsedItem(item.source, source_uri, cleaned.document, len(cleaned.repaired))


def _build(parsed: ParsedItem, base_uri: str, context_version: int) -> PreparedRecord | Failure:
    document, source_uri = parsed.document, parsed.source_uri
    try:
        schema_name_for(document)
    except ValueError as error:
        return Failure(STAGE, parsed.source, UNKNOWN_TYPE, source_uri, [str(error)])
    record_id = local_record_id(source_uri, base_uri)
    alias: str | None = None
    if record_id is None:
        record_id = new_uuid7()
        alias = source_uri
        uri = record_uri(record_id, base_uri=base_uri)
        document = remint(document, source_uri, uri)
    else:
        uri = source_uri
    stored = assign_node_iris(document, uri)
    try:
        expand_to_ntriples(stored, context_version, record_uri=uri)
    except ExpansionError as error:
        detail = f"{type(error).__name__}: {error}"
        return Failure(STAGE, parsed.source, EXPANSION_FAILED, source_uri, [detail])
    findings = schema_findings(validate_schema(stored, uri, context_version=context_version))
    labels, default_label = labels_from_record(stored)
    return PreparedRecord(
        id=record_id,
        uri=uri,
        source_uri=source_uri,
        alias=alias,
        type=stored["type"],
        etag=record_etag(stored),
        data=canonical_json(stored).decode("utf-8"),
        labels=orjson.dumps(labels).decode("utf-8"),
        default_label=default_label,
        primary_classification=primary_classification(stored),
        findings=findings,
        source=parsed.source,
    )


def build(parsed: ParsedItem, base_uri: str, context_version: int) -> PreparedRecord | Failure:
    """Turn a parsed item into its rows, or the reason it is rejected (module docstring).

    Never raises for a bad document: anything unexpected is reported as ``LAE-BOOT-010``, so one
    record cannot stop a load of millions.
    """
    try:
        return _build(parsed, base_uri, context_version)
    except Exception as error:  # noqa: BLE001 - reported per record, see the docstring
        detail = f"{type(error).__name__}: {error}"
        return Failure(STAGE, parsed.source, PREPARE_FAILED, parsed.source_uri, [detail[:500]])


def prepare(
    item: SourceItem, base_uri: str, context_version: int
) -> tuple[PreparedRecord | None, Failure | None]:
    """``parse`` then ``build`` for one input item: ``(record, None)`` or ``(None, failure)``."""
    parsed = parse(item)
    if isinstance(parsed, Failure):
        return None, parsed
    built = build(parsed, base_uri, context_version)
    if isinstance(built, Failure):
        return None, built
    return built, None


def parse_batch(items: Iterable[SourceItem]) -> tuple[list[ParsedItem], list[Failure]]:
    """Parse a batch of input items: the records to build and the items that are not records."""
    parsed: list[ParsedItem] = []
    failures: list[Failure] = []
    for item in items:
        result = parse(item)
        if isinstance(result, Failure):
            failures.append(result)
        else:
            parsed.append(result)
    return parsed, failures


def build_batch(parsed: Iterable[ParsedItem], base_uri: str, context_version: int) -> PreparedBatch:
    """Build the rows of a batch of parsed items; rejected ones are returned as failures."""
    cpu0 = time.process_time()
    records: list[PreparedRecord] = []
    failures: list[Failure] = []
    for item in parsed:
        result = build(item, base_uri, context_version)
        if isinstance(result, Failure):
            failures.append(result)
        else:
            records.append(result)
    return PreparedBatch(records, failures, time.process_time() - cpu0)
