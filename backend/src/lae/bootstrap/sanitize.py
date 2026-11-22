"""Repairing the ids of incoming JSON-LD before it is stored or expanded (Spike S6, finding 2).

pyld silently drops an IRI that contains whitespace, and every triple that uses it, and
``lae.rdf.expand_to_ntriples`` therefore rejects such a record outright. Source data does contain
them (a space in a minted IRI, a line break where an IRI was wrapped). ``clean_ids`` puts every
``id`` through ``lae.rdf.sanitize.clean_iri``, which percent-encodes the characters N-Triples
forbids and deletes line breaks, the same repair LUX applies. An id it cannot repair (no scheme,
no host, a blank node label) is reported, never dropped.

``lae bootstrap load`` stores the repaired document, so the stored JSON-LD, the bulk export and
every later re-expansion (the sync worker's first edit of a bulk-loaded record, ADR-0007) agree.
``lae bootstrap export-nt`` applies it again before expansion, for records that did not come
through the load.
"""

from __future__ import annotations

from typing import Any, Final, NamedTuple

from lae.rdf.sanitize import clean_iri

_ID_KEYS: Final = ("id", "@id")
_CONTEXT_KEY: Final = "@context"


class CleanedIds(NamedTuple):
    """The document with repaired ids, the ids that were repaired, and those that cannot be."""

    document: dict[str, Any]
    repaired: list[str]
    invalid: list[str]


def _clean(value: Any, repaired: list[str], invalid: list[str]) -> Any:
    if isinstance(value, list):
        return [_clean(item, repaired, invalid) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key == _CONTEXT_KEY:
            out[key] = item
        elif key in _ID_KEYS and isinstance(item, str):
            cleaned = clean_iri(item)
            if cleaned is None:
                invalid.append(item)
                out[key] = item
            else:
                if cleaned != item:
                    repaired.append(item)
                out[key] = cleaned
        else:
            out[key] = _clean(item, repaired, invalid)
    return out


def clean_ids(jsonld: dict[str, Any]) -> CleanedIds:
    """Return a copy of ``jsonld`` whose every ``id`` (and ``@id``) is a writable IRI if possible.

    Values inside ``@context`` are left alone, as are ids that cannot be repaired: they are
    listed in ``invalid`` (in document order) and the caller decides. ``jsonld`` is not modified.
    """
    repaired: list[str] = []
    invalid: list[str] = []
    document = _clean(jsonld, repaired, invalid)
    return CleanedIds(document, repaired, invalid)
