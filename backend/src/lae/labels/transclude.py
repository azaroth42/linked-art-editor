"""Read-time label transclusion (spec §3.2).

``hydrate_labels`` sets ``_label`` on every reference whose target has an ``entity_labels`` row,
choosing the label for the caller's locale chain (``lae.api.deps.locale_chain``): for each
language range in order, RFC 4647 basic filtering picks a label whose tag equals the range or
starts with it followed by ``-``; if no range matches, ``default_label`` is used. References whose
target has no row keep whatever ``_label`` they have.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import Text, any_, bindparam, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from lae.db.models import EntityLabel
from lae.labels.normalize import LABEL_KEY
from lae.labels.refs import reference_ids, rewrite_references

_LABEL_ROWS = select(EntityLabel.uri, EntityLabel.labels, EntityLabel.default_label).where(
    EntityLabel.uri == any_(bindparam("uris", type_=ARRAY(Text)))
)


def match_label(labels: Mapping[str, str], locale_chain: Sequence[str]) -> str | None:
    """Return the label for the first range in ``locale_chain`` that matches a tag, else ``None``.

    Matching is RFC 4647 basic filtering, case-insensitive: range ``en`` matches ``en`` and
    ``en-GB``, but ``en-GB`` does not match ``en``. An exact match beats a longer tag; among
    longer tags the alphabetically first wins, so the choice is deterministic.
    """
    tags = sorted(labels, key=str.lower)
    for language_range in locale_chain:
        wanted = language_range.lower()
        exact = [tag for tag in tags if tag.lower() == wanted]
        if exact:
            return labels[exact[0]]
        for tag in tags:
            if tag.lower().startswith(wanted + "-"):
                return labels[tag]
    return None


def resolve_label(
    labels: Mapping[str, str], default_label: str, locale_chain: Sequence[str]
) -> str:
    """Return the label for ``locale_chain`` from an ``entity_labels`` row, else its default."""
    match = match_label(labels, locale_chain)
    return default_label if match is None else match


async def hydrate_labels(
    jsonld: Mapping[str, Any], locale_chain: Sequence[str], session: AsyncSession
) -> dict[str, Any]:
    """Return a copy of ``jsonld`` with ``_label`` transcluded on its references (one query)."""
    uris = reference_ids(jsonld)
    resolved: dict[str, str] = {}
    if uris:
        rows = await session.execute(_LABEL_ROWS, {"uris": sorted(uris)})
        for uri, labels, default_label in rows:
            resolved[uri] = resolve_label(labels, default_label, locale_chain)

    def transclude(reference: dict[str, Any]) -> dict[str, Any]:
        label = resolved.get(reference["id"])
        if label is not None:
            reference[LABEL_KEY] = label
        return reference

    return rewrite_references(jsonld, transclude)
