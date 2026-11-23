"""``entity_labels.usage_count``: how often published records point at each entity (Step 2.1).

``refresh_usage_counts`` asks QLever, once per class of the entities in ``entity_labels``
("chunked by type"),

```sparql
SELECT ?o (COUNT(?s) AS ?n) WHERE {
  ?o rdf:type <class> .
  GRAPH ql:default-graph { ?s ?p ?o }
} GROUP BY ?o
```

the number of triples of the default graph, where every published record lives, whose object is
an entity of that class: references from records and from their embedded nodes. Sidecar graphs do
not count, so an AAT concept is not "used" because another AAT concept is narrower than it. The
``rdf:type`` may come from any graph, which is how a sidecar entity's class is known. The counts
are written with one ``UPDATE … FROM`` that also sets every entity not counted to 0; only rows
whose count changes are written, and ``updated_at`` is left alone. Term pickers
(``GET /api/vocab/{id}/terms``) and autocomplete order by the column.

It is a nightly job of the ``scheduler`` process (Step 1.15 schedules it; until then
``lae vocab refresh-usage`` runs it).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Final, cast

from sqlalchemy import CursorResult, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lae.db.models import EntityLabel
from lae.sparql.terms import iri
from lae.sync.qlever_client import QLeverClient

JOB_NAME: Final = "refresh_usage_counts"
"""The scheduler job's name (Step 1.15)."""

DEFAULT_GRAPH: Final = "http://qlever.cs.uni-freiburg.de/builtin-functions/default-graph"
"""QLever's name for the graph of triples loaded or inserted without one."""

RDF_TYPE: Final = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
WRITE_CHUNK: Final = 50_000
"""Counts sent to Postgres per statement."""

_CREATE_STAGE: Final = text(
    "CREATE TEMPORARY TABLE usage_count_stage (uri text PRIMARY KEY, n integer NOT NULL) "
    "ON COMMIT DROP"
)
_STAGE: Final = text(
    "INSERT INTO usage_count_stage (uri, n) "
    "SELECT uri, n FROM unnest(CAST(:uris AS text[]), CAST(:counts AS integer[])) AS c (uri, n)"
)
_APPLY: Final = text(
    """
UPDATE entity_labels AS e
SET usage_count = COALESCE(c.n, 0)
FROM entity_labels AS f
LEFT JOIN usage_count_stage AS c ON c.uri = f.uri
WHERE e.uri = f.uri AND e.usage_count IS DISTINCT FROM COALESCE(c.n, 0)
"""
)


@dataclass(slots=True)
class UsageSummary:
    """What ``refresh_usage_counts`` did."""

    classes: dict[str, int] = field(default_factory=dict)
    """Entity class → how many entities of it QLever counted."""
    updated: int = 0
    """``entity_labels`` rows whose ``usage_count`` changed."""

    def as_json(self) -> dict[str, Any]:
        """The summary ``lae vocab refresh-usage`` prints."""
        return {"job": JOB_NAME, "classes": self.classes, "updated": self.updated}


def build_usage_query(class_iri: str) -> str:
    """The count query for the entities of one class (see the module docstring)."""
    return (
        f"SELECT ?o (COUNT(?s) AS ?n) WHERE {{\n"
        f"  ?o {iri(RDF_TYPE)} {iri(class_iri)} .\n"
        f"  GRAPH {iri(DEFAULT_GRAPH)} {{ ?s ?p ?o }}\n"
        f"}} GROUP BY ?o"
    )


def _chunks(items: list[tuple[str, int]], size: int) -> Iterable[list[tuple[str, int]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def refresh_usage_counts(
    session: AsyncSession, qlever: QLeverClient, *, context_version: int = 1
) -> UsageSummary:
    """Recount ``entity_labels.usage_count`` from QLever (in the caller's transaction).

    Raises ``QLeverError`` if QLever fails; nothing is written then.
    """
    from lae.bootstrap.export import class_iri

    summary = UsageSummary()
    counts: dict[str, int] = {}
    types = sorted(set(await session.scalars(select(EntityLabel.type).distinct())))
    for type_name in types:
        klass = class_iri(type_name, context_version)
        if klass is None:
            continue
        rows = await qlever.query(build_usage_query(klass))
        found = 0
        for row in rows:
            uri, n = row.get("o"), row.get("n")
            if isinstance(uri, str) and isinstance(n, int):
                # An entity with two classes is counted twice over the same triples.
                counts[uri] = max(counts.get(uri, 0), n)
                found += 1
        summary.classes[type_name] = found
    await session.execute(_CREATE_STAGE)
    for chunk in _chunks(sorted(counts.items()), WRITE_CHUNK):
        await session.execute(
            _STAGE, {"uris": [uri for uri, _ in chunk], "counts": [n for _, n in chunk]}
        )
    result = await session.execute(_APPLY)
    summary.updated = cast(CursorResult[Any], result).rowcount
    await session.execute(text("DROP TABLE usage_count_stage"))
    return summary
