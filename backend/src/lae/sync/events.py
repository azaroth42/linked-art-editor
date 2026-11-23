"""Translation of one outbox event into the SPARQL Update that applies it (Step 1.11 contract §2.1).

An event says that a record went from ``from_version`` to ``to_version``. Its update is built
**only from ``record_revisions``** (the stored canonical N-Triples and JSON-LD of those versions)
and ``records.uri``: ``records.data`` is never read and nothing is expanded, so the translation
of an event is the same whenever it is made, however many versions were published since
(contract §3 item 1). The worker trusts ``payload.drift_check`` as publish computed it.

There are four modes (``choose_mode``, item 2):

- ``INSERT``: a first publish (``from_version`` NULL). ``INSERT DATA`` of every line of
  ``to_version``; no revision 0 is looked up.
- ``DIFF``: ``DELETE DATA (old - new) ; INSERT DATA (new - old)`` between the stored lines of
  ``from_version`` and ``to_version``, pasted unchanged. Valid only when QLever holds exactly the
  lines of ``from_version`` (item 6); an empty diff is ``""``, nothing to send.
- ``REPLACE``: ``DELETE WHERE { <s> ?p ?o }`` for every subject the record may have in QLever,
  then ``INSERT DATA`` of every line of ``to_version``. Chosen when ``drift_check`` is set,
  when the stored lines of ``from_version`` are empty (a bulk-loaded version that could not be
  expanded), or when the record's previous event is a dead letter (``unhealed``, item 5).
- ``RETRACT``: the ``DELETE WHERE`` part alone, for a ``delete`` event.

The subjects retracted (item 4) are the union, over every revision ``v`` with
``lo <= v <= from_version``, of ``owned_subjects(jsonld_v, uri)`` and the subjects of the stored
lines of ``v``. ``lo`` is ``from_version``, lowered to the smallest
``coalesce(from_version, to_version)`` of the run of ``failed`` events of the record directly
before this one: those were never applied, so QLever may still hold any version from ``lo`` on.
The subjects never come from ``to_version`` alone or from the current data, so a node the edit
removed is still retracted.

``translate_event`` raises ``EventError`` (``lae.sync.errors``) with ``LAE-SYNC-001`` (a revision
or the record row is missing), ``-002`` (stored bytes unreadable, a subject not an IRI) or
``-003`` (an event this step cannot translate).
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

import asyncpg
import orjson
import zstandard

from lae.db.models import OutboxEventKind
from lae.rdf import decompress, diff_triples, owned_subjects, parse_ntriples
from lae.sparql.terms import iri
from lae.sparql.updates import build_delete_where_subjects, build_update
from lae.sync.errors import REVISION_MISSING, UNREADABLE, UNTRANSLATABLE, EventError

UPDATE_SEPARATOR: Final = " ;\n"
"""Between two operations of one update, and between the updates of a batch."""

EVENT_COLUMNS: Final = (
    "id, kind::text AS kind, record_id, from_version, to_version, payload::text AS payload, "
    "attempts, created_at"
)
"""The ``outbox_events`` columns ``event_from_row`` reads."""

_DETAIL_CHARS: Final = 500


class Mode(StrEnum):
    """How one event is applied to QLever."""

    INSERT = "insert"  # INSERT DATA of every to_version triple
    DIFF = "diff"  # DELETE DATA (old - new) ; INSERT DATA (new - old)
    REPLACE = "replace"  # DELETE WHERE per subject ; INSERT DATA of every to_version triple
    RETRACT = "retract"  # DELETE WHERE per subject


@dataclass(frozen=True, slots=True)
class Event:
    """One ``outbox_events`` row, as read."""

    id: int
    kind: OutboxEventKind
    record_id: UUID
    from_version: int | None
    to_version: int | None
    drift_check: bool  # payload.get("drift_check") is True
    attempts: int
    created_at: datetime


def event_from_row(row: Mapping[str, Any]) -> Event:
    """Build an ``Event`` from a row selected with ``EVENT_COLUMNS``."""
    payload = json.loads(row["payload"]) if row["payload"] is not None else {}
    drift_check = isinstance(payload, dict) and payload.get("drift_check") is True
    return Event(
        id=row["id"],
        kind=OutboxEventKind(row["kind"]),
        record_id=row["record_id"],
        from_version=row["from_version"],
        to_version=row["to_version"],
        drift_check=drift_check,
        attempts=row["attempts"],
        created_at=row["created_at"],
    )


def _untranslatable(event: Event) -> EventError:
    return EventError(UNTRANSLATABLE, event=event.id, kind=event.kind.value)


def check_translatable(event: Event) -> None:
    """Raise ``LAE-SYNC-003`` for an event this step cannot translate (before any read)."""
    if event.kind == OutboxEventKind.PUBLISH and event.to_version is None:
        raise _untranslatable(event)
    if event.kind == OutboxEventKind.DELETE and event.from_version is None:
        raise _untranslatable(event)
    if event.kind not in (OutboxEventKind.PUBLISH, OutboxEventKind.DELETE):
        raise _untranslatable(event)


def choose_mode(event: Event, *, unhealed: bool, old_lines_empty: bool) -> Mode:
    """Return the mode of ``event`` (contract §3 item 2); ``LAE-SYNC-003`` if there is none.

    ``unhealed``: the record's previous event is a dead letter (item 5). ``old_lines_empty``:
    the stored N-Triples of ``from_version`` are empty. Both are ignored where they cannot
    matter (a first publish has nothing to retract; a delete always retracts).
    """
    check_translatable(event)
    if event.kind == OutboxEventKind.DELETE:
        return Mode.RETRACT
    if event.from_version is None:
        return Mode.INSERT
    if event.drift_check or unhealed or old_lines_empty:
        return Mode.REPLACE
    return Mode.DIFF


def subjects_of(lines: Iterable[str]) -> set[str]:
    """Return the subject IRI of each canonical N-Triples line (``<s> <p> <o> .``)."""
    return {line[1 : line.index(">")] for line in lines}


def build_event_update(
    mode: Mode,
    *,
    old_lines: frozenset[str],
    new_lines: frozenset[str],
    retract_subjects: Collection[str],
) -> str:
    """Return the SPARQL Update of one event, or ``""`` when there is nothing to send.

    ``old_lines`` is used by ``DIFF`` only, ``new_lines`` by every mode but ``RETRACT``,
    ``retract_subjects`` by ``REPLACE`` and ``RETRACT``. In ``REPLACE`` the ``DELETE WHERE``
    operations come first: the insert must survive them. Raises ``ValueError`` for a line that is
    not canonical or a subject that is not a writable IRI.
    """
    if mode == Mode.INSERT:
        return build_update((), new_lines)
    if mode == Mode.DIFF:
        return build_update(*diff_triples(old_lines, new_lines))
    retract = build_delete_where_subjects(retract_subjects)
    if mode == Mode.RETRACT:
        return retract
    return UPDATE_SEPARATOR.join(part for part in (retract, build_update((), new_lines)) if part)


# -- reading Postgres ---------------------------------------------------------------------------


def _unreadable(event: Event, version: int | None, error: Exception) -> EventError:
    return EventError(
        UNREADABLE,
        event=event.id,
        record_id=event.record_id,
        version=version,
        detail=f"{type(error).__name__}: {error}"[:_DETAIL_CHARS],
    )


def _missing(event: Event, version: int | None) -> EventError:
    return EventError(REVISION_MISSING, event=event.id, record_id=event.record_id, version=version)


def _read_lines(event: Event, version: int, blob: bytes) -> frozenset[str]:
    try:
        return parse_ntriples(decompress(blob))
    except (zstandard.ZstdError, ValueError) as error:  # UnicodeDecodeError is a ValueError
        raise _unreadable(event, version, error) from error


def _read_jsonld(event: Event, version: int, blob: bytes) -> dict[str, Any]:
    try:
        document = orjson.loads(decompress(blob))
    except (zstandard.ZstdError, ValueError) as error:  # orjson.JSONDecodeError is a ValueError
        raise _unreadable(event, version, error) from error
    if not isinstance(document, dict):
        raise _unreadable(event, version, ValueError("the JSON-LD is not an object"))
    return document


async def _record_uri(conn: asyncpg.Connection, event: Event) -> str:
    uri: str | None = await conn.fetchval("SELECT uri FROM records WHERE id = $1", event.record_id)
    if uri is None:
        raise _missing(event, None)
    return uri


async def _lines_of(conn: asyncpg.Connection, event: Event, version: int) -> frozenset[str]:
    blob = await conn.fetchval(
        "SELECT ntriples_zstd FROM record_revisions WHERE record_id = $1 AND version = $2",
        event.record_id,
        version,
    )
    if blob is None:
        raise _missing(event, version)
    return _read_lines(event, version, blob)


async def _dead_letter_floor(conn: asyncpg.Connection, event: Event) -> int | None:
    """``None`` unless the record's previous event is ``failed`` (the event is ``unhealed``).

    Otherwise the smallest ``coalesce(from_version, to_version)`` of the run of ``failed`` events
    of the record directly before this one, i.e. of those after its last event in any other
    status (contract §3 items 4 and 5).
    """
    row = await conn.fetchrow(
        """
        SELECT count(*) AS failed, min(coalesce(from_version, to_version)) AS floor
        FROM outbox_events
        WHERE record_id = $1 AND id < $2 AND status = 'failed'
          AND id > coalesce(
            (SELECT max(id) FROM outbox_events
             WHERE record_id = $1 AND id < $2 AND status <> 'failed'),
            0)
        """,
        event.record_id,
        event.id,
    )
    if row is None or row["failed"] == 0:
        return None
    floor: int | None = row["floor"]
    return floor


async def _retract_subjects(
    conn: asyncpg.Connection, event: Event, uri: str, low: int, high: int
) -> set[str]:
    """Every subject of revisions ``low..high`` (owned subjects of the JSON-LD, line subjects)."""
    rows = await conn.fetch(
        "SELECT version, ntriples_zstd, jsonld_zstd FROM record_revisions "
        "WHERE record_id = $1 AND version BETWEEN $2 AND $3 ORDER BY version",
        event.record_id,
        low,
        high,
    )
    found = {row["version"] for row in rows}
    for version in range(low, high + 1):
        if version not in found:
            raise _missing(event, version)
    subjects: set[str] = set()
    for row in rows:
        version = row["version"]
        of_version = owned_subjects(_read_jsonld(event, version, row["jsonld_zstd"]), uri)
        of_version |= subjects_of(_read_lines(event, version, row["ntriples_zstd"]))
        for subject in of_version:
            try:
                iri(subject)
            except ValueError as error:
                raise _unreadable(event, version, error) from error
        subjects |= of_version
    return subjects


async def translate_event(conn: asyncpg.Connection, event: Event) -> str:
    """Return the SPARQL Update of ``event``, or ``""`` if there is nothing to send.

    Only reads Postgres (``records.uri``, ``record_revisions``, earlier ``outbox_events`` of the
    record). Raises ``EventError``.
    """
    check_translatable(event)
    uri = await _record_uri(conn, event)
    floor = None
    if event.from_version is not None:
        floor = await _dead_letter_floor(conn, event)
    new_lines: frozenset[str] = frozenset()
    if event.kind == OutboxEventKind.PUBLISH:
        assert event.to_version is not None  # check_translatable
        new_lines = await _lines_of(conn, event, event.to_version)
    old_lines: frozenset[str] = frozenset()
    if event.kind == OutboxEventKind.PUBLISH and event.from_version is not None:
        old_lines = await _lines_of(conn, event, event.from_version)
    mode = choose_mode(event, unhealed=floor is not None, old_lines_empty=not old_lines)
    subjects: set[str] = set()
    if mode in (Mode.REPLACE, Mode.RETRACT):
        assert event.from_version is not None  # REPLACE and RETRACT need a previous version
        low = event.from_version if floor is None else min(event.from_version, floor)
        subjects = await _retract_subjects(conn, event, uri, low, event.from_version)
    try:
        return build_event_update(
            mode, old_lines=old_lines, new_lines=new_lines, retract_subjects=subjects
        )
    except ValueError as error:  # already checked; kept as a diagnostic rather than a crash
        raise _unreadable(event, event.to_version, error) from error
