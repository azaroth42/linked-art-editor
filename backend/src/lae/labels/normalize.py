"""Write-time stub normalization (spec §3.2; decision A11, ADR-0006).

Before a record is stored, ``_label`` is removed from every reference whose target has an
``entity_labels`` row, because the label is transcluded on read. A reference to a target with no
row (a pasted external URI, say) keeps its ``_label``: it is the only label there is. Which
objects are references is defined in ``lae.labels.refs`` (ADR-0019).

Usage: ``normalize_refs(jsonld, await known_reference_ids(session, jsonld))``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from sqlalchemy import Text, any_, bindparam, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from lae.db.models import EntityLabel
from lae.labels.refs import reference_ids, rewrite_references

LABEL_KEY = "_label"

_KNOWN_URIS = select(EntityLabel.uri).where(
    EntityLabel.uri == any_(bindparam("uris", type_=ARRAY(Text)))
)


async def known_uris(session: AsyncSession, uris: Collection[str]) -> set[str]:
    """Return those of ``uris`` that have an ``entity_labels`` row, in one query (none if empty).

    Every row counts, whatever its ``status`` or ``source``: ``hydrate_labels`` reads the same
    rows, so a label stripped here is always restored on read.
    """
    if not uris:
        return set()
    result = await session.execute(_KNOWN_URIS, {"uris": sorted(uris)})
    return set(result.scalars())


async def known_reference_ids(session: AsyncSession, jsonld: Mapping[str, Any]) -> set[str]:
    """Return the reference targets of ``jsonld`` that have an ``entity_labels`` row."""
    return await known_uris(session, reference_ids(jsonld))


def normalize_refs(jsonld: Mapping[str, Any], known: set[str]) -> dict[str, Any]:
    """Return a copy of ``jsonld`` without ``_label`` on references whose ``id`` is in ``known``.

    Everything else is kept as it is: the root, embedded nodes (including their ``_label``),
    ``@context``, and references to unknown targets. ``jsonld`` is not modified.
    """

    def strip_known(reference: dict[str, Any]) -> dict[str, Any]:
        if reference["id"] in known:
            reference.pop(LABEL_KEY, None)
        return reference

    return rewrite_references(jsonld, strip_known)
