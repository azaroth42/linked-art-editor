"""Y.Doc to JSON-LD: projection and stripping (R11, R13, R15 of the S3 rule list)."""

from __future__ import annotations

import math
from typing import Any

from pycrdt import Doc, Map, Text

from lae.ids import is_internal_node_iri
from lae.projection.hydrate import resolve_record_uri
from lae.projection.rules import (
    OPAQUE_KEYS,
    ORDER_KEY,
    PROJ_BAD_STATE,
    RESERVED_KEYS,
    ROOT_MAP,
    VALUE_KEY,
    ProjectionError,
    decode_plain,
    parse_key,
)


def record_map(doc: Doc[Any]) -> Map[Any]:
    """The root ``record`` map of a document (R1)."""
    root: Map[Any] = doc.get(ROOT_MAP, type=Map)
    return root


def entry_order(entry: Any) -> float | None:
    """The numeric ``_order`` of an entry map, or ``None`` if it has none."""
    if not isinstance(entry, Map) or ORDER_KEY not in entry:
        return None
    order = entry[ORDER_KEY]
    if isinstance(order, bool) or not isinstance(order, int | float) or math.isnan(order):
        return None
    return float(order)


def _sort(entries: list[tuple[str, Any]]) -> list[tuple[str, Map[Any]]]:
    # R13: by _order, then by the whole entry key in Unicode code-point order (Python str order).
    def sort_key(item: tuple[str, Any]) -> tuple[float, str]:
        order = entry_order(item[1])
        if order is None:
            raise ProjectionError(PROJ_BAD_STATE, f"entry {item[0]!r} has no numeric _order")
        return order, item[0]

    return sorted(entries, key=sort_key)


def sorted_entries(parent: Map[Any], prop: str) -> list[tuple[str, Map[Any]]]:
    """The entries of property ``prop`` of an object map as ``(entry key, entry)``, R13 order."""
    found: list[tuple[str, Any]] = []
    for key, value in parent.items():
        key_prop, item_key = parse_key(key)
        if item_key is not None and key_prop == prop:
            found.append((key, value))
    return _sort(found)


def project(doc: Doc[Any]) -> dict[str, Any]:
    """Project the ``record`` map back to JSON-LD (R11); internal node IRIs are kept."""
    return project_object(record_map(doc))


def project_object(obj: Map[Any]) -> dict[str, Any]:
    """Project one object map (the root, a node or a reference) to a JSON object."""
    out: dict[str, Any] = {}
    arrays: dict[str, list[tuple[str, Any]]] = {}
    for key, value in obj.items():
        prop, item_key = parse_key(key)
        if item_key is not None:
            arrays.setdefault(prop, []).append((key, value))
        elif key not in RESERVED_KEYS:
            out[prop] = project_value(value)
    # Entries win over a single value stored for the same property (R11).
    for prop, entries in arrays.items():
        out[prop] = [project_entry(entry) for _, entry in _sort(entries)]
    return out


def project_value(value: Any) -> Any:
    """Project a value stored under a property key: a ``Text``, an object map or a plain value."""
    if isinstance(value, Text):
        return str(value)
    if isinstance(value, Map):
        return project_object(value)
    return decode_plain(value)


def project_entry(entry: Map[Any]) -> Any:
    """Project one array entry: its decoded ``_value`` if it has one, else an object."""
    if VALUE_KEY in entry:
        return decode_plain(entry[VALUE_KEY])
    return project_object(entry)


def strip_internal_iris(jsonld: dict[str, Any], record_uri: str | None = None) -> dict[str, Any]:
    """Return a copy without any ``id`` that is an internal node IRI of this record (R15).

    ``record_uri`` defaults to the root ``id``, which is never removed. Nothing inside
    ``@context`` is touched.
    """
    uri = resolve_record_uri(record_uri, jsonld.get("id"))
    return {k: v if k in OPAQUE_KEYS else _strip(v, uri) for k, v in jsonld.items()}


def _strip(value: Any, uri: str) -> Any:
    if isinstance(value, dict):
        return {
            k: v if k in OPAQUE_KEYS else _strip(v, uri)
            for k, v in value.items()
            if not (k == "id" and is_internal_node_iri(v, uri))
        }
    if isinstance(value, list):
        return [_strip(v, uri) for v in value]
    return value
