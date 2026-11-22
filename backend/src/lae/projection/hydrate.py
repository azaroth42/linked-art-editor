"""JSON-LD to Y.Doc: internal node IRIs and hydration (R1-R10 of ``docs/spikes/S3-projection.md``).

A record is hydrated once per draft epoch, by the server; every other replica receives that
state through sync (R2). Two hydrations of the same JSON-LD must never be merged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pycrdt import Doc, Map, Text

from lae.ids import node_iri
from lae.projection.rules import (
    OPAQUE_KEYS,
    ORDER_KEY,
    PROJ_BAD_STATE,
    PROJ_NO_RECORD_URI,
    PROJ_RESERVED_KEY,
    RESERVED_KEYS,
    ROOT_MAP,
    TEXT_CONTENT_TYPES,
    VALUE_KEY,
    ProjectionError,
    check_text,
    encode_plain,
    entry_key,
    is_structured,
    property_key,
    unique_key,
    value_key,
)

Mint = Callable[[], str]
"""Returns a fresh internal node IRI on every call."""


def resolve_record_uri(record_uri: str | None, root_id: Any) -> str:
    """Return ``record_uri``, or the root ``id`` when it is ``None``; else ``LAE-PROJ-004``."""
    uri = record_uri if record_uri is not None else root_id
    if not isinstance(uri, str) or not uri:
        raise ProjectionError(PROJ_NO_RECORD_URI, "no record URI: pass record_uri or set a root id")
    return uri


def assign_node_iris(
    jsonld: dict[str, Any], record_uri: str, mint: Mint | None = None
) -> dict[str, Any]:
    """Return a copy with ``id`` added to every embedded node that lacks one (R4).

    The root, references, nodes that already have an ``id``, and everything inside opaque
    values are left as they are. The input is not mutated. ``mint`` replaces
    ``lae.ids.node_iri`` in tests.
    """
    return assign_object(jsonld, mint or (lambda: node_iri(record_uri)), is_root=True)


def assign_object(obj: dict[str, Any], mint: Mint, *, is_root: bool = False) -> dict[str, Any]:
    """``assign_node_iris`` for one node or reference; the root itself never gets an ``id``."""
    out: dict[str, Any] = {}
    for key, value in obj.items():
        if key in OPAQUE_KEYS:
            out[key] = value
        elif is_structured(value):
            out[key] = assign_object(value, mint)
        elif isinstance(value, list):
            out[key] = [assign_object(i, mint) if is_structured(i) else i for i in value]
        else:
            out[key] = value
    if not is_root and "id" not in out and "type" in out:
        out["id"] = mint()
    return out


def hydrate(
    jsonld: dict[str, Any], record_uri: str | None = None, *, mint: Mint | None = None
) -> Doc[Any]:
    """Build a fresh ``Doc`` whose ``record`` map holds ``jsonld`` under rules R1-R10.

    ``record_uri`` defaults to the root ``id``. Nothing is created when the input is rejected.
    """
    if not isinstance(jsonld, dict):
        raise ProjectionError(PROJ_BAD_STATE, "a record must be a JSON object")
    uri = resolve_record_uri(record_uri, jsonld.get("id"))
    fields = object_fields(assign_node_iris(jsonld, uri, mint))
    doc: Doc[Any] = Doc()
    root = doc.get(ROOT_MAP, type=Map)
    with doc.transaction():
        for key, value in fields.items():
            root[key] = value
    return doc


def object_fields(obj: dict[str, Any]) -> dict[str, Any]:
    """The content of one object map: its property keys and its array entries (R5, R6, R8).

    Every embedded node must already have its ``id`` (``assign_node_iris``). The values are
    plain values and not-yet-integrated ``Map`` / ``Text`` objects, ready to be assigned.
    """
    fields: dict[str, Any] = {}
    for key, value in obj.items():
        if not isinstance(key, str):
            raise ProjectionError(PROJ_BAD_STATE, f"non-string key {key!r}")
        check_text(key, "a key")
        if key in RESERVED_KEYS:
            raise ProjectionError(PROJ_RESERVED_KEY, f"reserved key {key!r} in the input")
        if key not in OPAQUE_KEYS and isinstance(value, list) and value:
            fields.update(_array_entries(key, value))
        else:
            fields[property_key(key)] = property_value(key, value, obj.get("type"))
    return fields


def is_text_content(key: str, value: Any, parent_type: Any) -> bool:
    """True if ``value`` under ``key`` of an object of ``parent_type`` is a ``Text`` (R8.2)."""
    return (
        key == "content"
        and isinstance(value, str)
        and isinstance(parent_type, str)
        and parent_type in TEXT_CONTENT_TYPES
    )


def property_value(key: str, value: Any, parent_type: Any) -> Any:
    """The stored form of a property value that is not a non-empty array (R8.1-3, R8.5)."""
    if key in OPAQUE_KEYS:
        return encode_plain(value)
    if is_text_content(key, value, parent_type):
        return Text(check_text(value, "content"))
    if is_structured(value):
        return Map(object_fields(value))
    return encode_plain(value)


def entry_fields(item: Any) -> tuple[str, dict[str, Any]]:
    """``(base item key, entry content without _order)`` for one array item (R6, R7)."""
    if is_structured(item):
        item_id = item.get("id")
        if not isinstance(item_id, str):
            raise ProjectionError(PROJ_BAD_STATE, "an embedded node has no id: assign IRIs first")
        return item_id, object_fields(item)
    return value_key(item), {VALUE_KEY: encode_plain(item)}


def _array_entries(prop: str, items: list[Any]) -> dict[str, Map[Any]]:
    """One entry per item, keyed ``[prop, item key]`` in the parent's own map (R6, R7)."""
    used: set[str] = set()
    entries: dict[str, Map[Any]] = {}
    for index, item in enumerate(items):
        base, fields = entry_fields(item)
        fields[ORDER_KEY] = float(index)
        item_key = unique_key(base, used)
        used.add(item_key)
        entries[entry_key(prop, item_key)] = Map(fields)
    return entries
