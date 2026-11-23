"""Harvesting inline metatypes into ``entity_labels.metatype_uris`` (Phase 2 Step 2.1, ADR-0029).

A Linked Art record often repeats a concept's metatype where it references the concept:

```json
"classified_as": [{"id": "http://vocab.getty.edu/aat/300111175", "type": "Type",
                   "classified_as": [{"id": "http://vocab.getty.edu/aat/300379842"}]}]
```

says that Dutch (300111175) is a Nationality (300379842). The record does not own the concept, so
none of this reaches QLever (ADR-0021): only the concept's own record or sidecar graph states its
metatype there. Rule-backed Value Sets that select concepts by metatype would then miss every
concept an institution typed only inline. So, as LUX's ``reference_manager.walk_for_refs`` does,
the metatypes seen inline are collected and added to the concept's ``entity_labels`` row, where
``lae.vocab.membership`` and the Step 2.2 validation rules read them.

**What is harvested.** Every object in a record, the root included, whose ``type`` is a concept
class (``CONCEPT_CLASSES``), whose ``id`` is a string, and that has ``classified_as``: each
``classified_as`` entry's string ``id`` is a metatype of that ``id``. For a concept record's root
these are its own metatypes. ``classified_as`` on a ``Person``, ``Group``, ``Place`` or object is
a classification, not a metatype, and is not harvested.

**How.** The harvest only adds: a metatype is never removed because a record stopped repeating it
(the concept owns its metatypes; ADR-0029 discusses this). Only existing rows are changed: a concept
without an ``entity_labels`` row is unresolved (ADR-0006) and gets its metatypes when a later
harvest finds its row. Neither ``updated_at`` nor anything else in the row changes, so the
autocomplete overlay is unaffected.

- ``harvest_record_metatypes`` runs for one record inside the publish transaction (it is called by
  ``lae.labels.upsert.upsert_entity_labels_for_record``).
- ``harvest_all_metatypes`` runs one SQL statement over every active record. ``lae bootstrap
  load`` and ``lae bootstrap aat-labels`` call it at the end; ``lae vocab harvest-metatypes`` runs
  it on demand, e.g. after a sidecar is loaded. Its JSON path expression selects exactly what
  ``inline_metatypes`` does (``tests/vocab/test_metatypes.py`` checks both over the corpus).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Final, cast

import asyncpg
from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

CONCEPT_CLASSES: Final = frozenset({"Type", "Language", "Material", "MeasurementUnit", "Currency"})
"""Linked Art concept classes ``Type`` and subclasses; their ``classified_as`` is a metatype."""

CLASSIFIED_AS: Final = "classified_as"

_CLASS_TEST: Final = " || ".join(f'@.type == "{name}"' for name in sorted(CONCEPT_CLASSES))
CONCEPT_PATH: Final = f'strict $.** ? ({_CLASS_TEST}) ? (@.id.type() == "string")'
"""Every object of a document, the root included, of a concept class with a string ``id``."""

METATYPE_PATH: Final = 'lax $.classified_as[*].id ? (@.type() == "string")'
"""The string ``id`` of every ``classified_as`` entry of one object (an array or one object)."""

_UNION: Final = (
    "ARRAY(SELECT DISTINCT m FROM unnest(e.metatype_uris || h.metatypes) AS m ORDER BY m)"
)

HARVEST_ALL_SQL: Final = f"""
WITH found AS (
    SELECT concept ->> 'id' AS uri, metatype #>> '{{}}' AS metatype
    FROM records AS r
    CROSS JOIN LATERAL jsonb_path_query(r.data, '{CONCEPT_PATH}', '{{}}', true) AS concept
    CROSS JOIN LATERAL jsonb_path_query(concept, '{METATYPE_PATH}', '{{}}', true) AS metatype
    WHERE r.status = 'active'
), h AS (
    SELECT uri, array_agg(DISTINCT metatype) AS metatypes FROM found GROUP BY uri
)
UPDATE entity_labels AS e
SET metatype_uris = {_UNION}
FROM h
WHERE e.uri = h.uri AND NOT e.metatype_uris @> h.metatypes
"""

HARVEST_SQL: Final = f"""
WITH h AS (
    SELECT uri, array_agg(DISTINCT metatype) AS metatypes
    FROM unnest(CAST(:uris AS text[]), CAST(:metatypes AS text[])) AS pairs (uri, metatype)
    GROUP BY uri
)
UPDATE entity_labels AS e
SET metatype_uris = {_UNION}
FROM h
WHERE e.uri = h.uri AND NOT e.metatype_uris @> h.metatypes
"""


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _objects(value: Any) -> Iterator[Mapping[str, Any]]:
    """Every JSON object in ``value``, itself included, depth first."""
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for item in value:
            yield from _objects(item)


def inline_metatypes(jsonld: Mapping[str, Any]) -> dict[str, set[str]]:
    """Return concept URI → the metatype URIs ``jsonld`` gives it (see the module docstring)."""
    found: dict[str, set[str]] = {}
    for node in _objects(jsonld):
        concept = node.get("id")
        if node.get("type") not in CONCEPT_CLASSES or not isinstance(concept, str):
            continue
        for entry in _as_list(node.get(CLASSIFIED_AS)):
            if isinstance(entry, Mapping) and isinstance(entry.get("id"), str):
                found.setdefault(concept, set()).add(entry["id"])
    return found


async def harvest_record_metatypes(session: AsyncSession, jsonld: Mapping[str, Any]) -> int:
    """Add the metatypes ``jsonld`` states inline to existing rows; return how many changed."""
    pairs = sorted(
        (uri, metatype)
        for uri, metatypes in inline_metatypes(jsonld).items()
        for metatype in metatypes
    )
    if not pairs:
        return 0
    result = await session.execute(
        text(HARVEST_SQL),
        {"uris": [uri for uri, _ in pairs], "metatypes": [metatype for _, metatype in pairs]},
    )
    return cast(CursorResult[Any], result).rowcount


async def harvest_all_metatypes(conn: asyncpg.Connection) -> int:
    """Add the inline metatypes of every active record; return how many rows changed."""
    status: str = await conn.execute(HARVEST_ALL_SQL)
    return int(status.rsplit(" ", 1)[-1])
