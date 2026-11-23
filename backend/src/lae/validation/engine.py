"""The validation pipeline: JSON Schema, then Python rules and SHACL (spec §5; ADR-0008).

``validate`` is what the API, publish (Step 1.10) and agents (Phase 3) call:

1. **JSON Schema** (``lae.validation.schema``). Any violation ends the run: SHACL and the rules
   assume the structure the schema guarantees.
2. **Rules** (``lae.validation.rules``) and **SHACL** (``lae.validation.shacl``), on the document
   with internal node IRIs. A document that arrives without them (a pasted or imported record)
   gets temporary ones, which changes no path. A record that cannot be expanded to RDF is
   reported with an ``LAE-RDF-*`` violation, and each ``id`` that expansion has to
   percent-encode with the warning ``LAE-RDF-006`` (ADR-0028).

The result is ``Diagnostics``: every finding, ordered, with ``ok`` true when none is a
violation. Paths are JSON Pointers into the document that was passed in.

Two facts come from the database and are looked up first, in two queries: the class of every
referenced entity (``entity_labels.type``, for the SHACL data graph) and which referenced records
are deleted (``LAE-RULE-004``). The rest is CPU work in ``validate_document``, a pure function:
``validate`` runs it in a thread so the event loop stays free, and ``validate_many`` runs it in a
process pool, because threads give no speed-up for this work (``docs/spikes/S4-shacl.md``,
finding 7).
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import Text, any_, bindparam, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from lae.db.models import EntityLabel, Record, RecordStatus
from lae.labels.refs import reference_ids
from lae.projection.hydrate import assign_node_iris
from lae.settings import get_settings
from lae.validation.diagnostics import Diagnostic, Diagnostics, has_no_violation, ordered
from lae.validation.expansion import iri_repair_warnings
from lae.validation.rules import validate_rules
from lae.validation.schema import schema_validator, validate_schema
from lae.validation.schema_map import PROVENANCE_SCHEMA, SCHEMA_BY_TYPE
from lae.validation.shacl import shapes_graph, validate_shacl

POOL_THRESHOLD: Final = 16
"""Batches smaller than this are validated in one thread: starting workers costs more."""

_REFERENCED_TYPES: Final = select(EntityLabel.uri, EntityLabel.type).where(
    EntityLabel.uri == any_(bindparam("uris", type_=ARRAY(Text)))
)
_DELETED_RECORDS: Final = select(Record.uri).where(
    Record.uri == any_(bindparam("uris", type_=ARRAY(Text))),
    Record.status == RecordStatus.DELETED,
)


@dataclass(frozen=True)
class ReferenceFacts:
    """What the database knows about the entities a document refers to."""

    types: Mapping[str, str] = field(default_factory=dict)
    """Referenced URI -> class name, from ``entity_labels.type``."""
    deleted: frozenset[str] = frozenset()
    """Referenced URIs of records whose ``status`` is ``deleted``."""


async def load_reference_facts(session: AsyncSession, uris: Collection[str]) -> ReferenceFacts:
    """Look up the class and the deleted status of ``uris`` (two queries; none if empty)."""
    if not uris:
        return ReferenceFacts()
    parameters = {"uris": sorted(uris)}
    types = {uri: name for uri, name in await session.execute(_REFERENCED_TYPES, parameters)}
    deleted = frozenset((await session.execute(_DELETED_RECORDS, parameters)).scalars())
    return ReferenceFacts(types, deleted)


def _references(jsonld: Any) -> set[str]:
    return reference_ids(jsonld) if isinstance(jsonld, Mapping) else set()


def validate_document(
    jsonld: Any,
    record_uri: str | None = None,
    facts: ReferenceFacts | None = None,
    *,
    context_version: int | None = None,
) -> Diagnostics:
    """Validate one document without touching the database (synchronous, CPU-bound).

    ``record_uri`` defaults to the root ``id``. ``facts`` is what ``load_reference_facts``
    returned for the document's references; without it no reference is known to be deleted and
    every reference has the class it declares itself.
    """
    facts = facts or ReferenceFacts()
    found: list[Diagnostic] = validate_schema(jsonld, record_uri, context_version=context_version)
    if not has_no_violation(found):
        return Diagnostics(items=ordered(found))
    root_id = jsonld.get("id")
    uri = record_uri if record_uri is not None else root_id if isinstance(root_id, str) else ""
    document = assign_node_iris(jsonld, uri) if uri else jsonld
    found.extend(iri_repair_warnings(jsonld, uri))
    found.extend(validate_rules(document, facts.deleted))
    found.extend(validate_shacl(document, uri, facts.types, context_version=context_version))
    return Diagnostics(items=ordered(found))


async def validate(
    jsonld: dict[str, Any],
    record_uri: str | None = None,
    session: AsyncSession | None = None,
    *,
    context_version: int | None = None,
) -> Diagnostics:
    """Validate a record: schema, then (if the schema passes) rules and SHACL.

    ``jsonld`` is the projected draft or any Linked Art record, with or without internal node
    IRIs; ``record_uri`` defaults to its root ``id``. With a ``session`` the classes of
    referenced entities and deleted targets come from the database; without one, references
    are taken at their word. Never raises for an invalid document.
    """
    facts = None
    if session is not None:
        facts = await load_reference_facts(session, _references(jsonld))
    return await asyncio.to_thread(
        validate_document, jsonld, record_uri, facts, context_version=context_version
    )


def warm_up() -> None:
    """Load the schemas and the shapes (once per process); raise if a file under profiles/ is bad.

    The API calls it at startup and every pool worker when it starts, so no validation pays
    for it and a broken shapes file is found before the first record.
    """
    for schema_name in sorted({*SCHEMA_BY_TYPE.values(), PROVENANCE_SCHEMA}):
        schema_validator(schema_name)
    shapes_graph()


_Job = tuple[Any, str | None, ReferenceFacts, int]


def _job(jsonld: Any, record_uri: str | None, facts: ReferenceFacts, version: int) -> _Job:
    """Pair a document with only the facts about its own references (jobs are pickled)."""
    uris = _references(jsonld)
    own = ReferenceFacts(
        {uri: facts.types[uri] for uri in uris if uri in facts.types}, facts.deleted & uris
    )
    return jsonld, record_uri, own, version


def _validate_job(job: _Job) -> Diagnostics:
    jsonld, record_uri, facts, context_version = job
    return validate_document(jsonld, record_uri, facts, context_version=context_version)


def _validate_sequentially(jobs: list[_Job]) -> list[Diagnostics]:
    return [_validate_job(job) for job in jobs]


def _validate_in_pool(jobs: list[_Job], workers: int) -> list[Diagnostics]:
    with ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=warm_up,
    ) as pool:
        return list(pool.map(_validate_job, jobs, chunksize=max(1, len(jobs) // (workers * 4))))


async def validate_many(
    documents: Sequence[tuple[dict[str, Any], str | None]],
    session: AsyncSession | None = None,
    *,
    max_workers: int | None = None,
    context_version: int | None = None,
) -> list[Diagnostics]:
    """Validate ``(jsonld, record_uri)`` pairs and return their diagnostics in the same order.

    Reference facts for the whole batch are read in one round trip. Batches of
    ``POOL_THRESHOLD`` documents or more run in a process pool of ``max_workers`` (default: the
    CPU count), started with ``spawn`` so that no event loop or thread state is forked; smaller
    ones, and ``max_workers=1``, run in one thread.
    """
    version = get_settings().context_version if context_version is None else context_version
    facts = ReferenceFacts()
    if session is not None:
        uris: set[str] = set()
        for jsonld, _ in documents:
            uris |= _references(jsonld)
        facts = await load_reference_facts(session, uris)
    jobs = [_job(jsonld, record_uri, facts, version) for jsonld, record_uri in documents]
    workers = min(max_workers or os.cpu_count() or 1, len(jobs))
    if len(jobs) < POOL_THRESHOLD or workers <= 1:
        return await asyncio.to_thread(_validate_sequentially, jobs)
    return await asyncio.to_thread(_validate_in_pool, jobs, workers)
