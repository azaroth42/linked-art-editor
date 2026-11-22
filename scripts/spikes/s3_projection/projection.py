"""Spike S3 prototype: lossless JSON-LD <-> Y.Doc projection (Phase 0, Step 0.8).

Throwaway code. The numbered rules it implements are in ``docs/spikes/S3-projection.md``;
comments cite them as ``R<n>``. Phase 1 Step 1.4 rewrites this under ``lae/projection/``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable
from typing import Any, Final

import orjson
import uuid_utils
from pycrdt import Doc, Map, Text

ROOT_MAP: Final = "record"
ORDER_KEY: Final = "_order"
VALUE_KEY: Final = "_value"
JSON_KEY: Final = "_json"
RESERVED_KEYS: Final = frozenset({ORDER_KEY, VALUE_KEY})
TEXT_CONTENT_TYPES: Final = frozenset({"Name", "Identifier", "LinguisticObject"})
REFERENCE_KEYS: Final = frozenset({"id", "type", "_label"})
OPAQUE_KEYS: Final = frozenset({"@context"})
MAX_SAFE_INT: Final = 2**53
INT_MIN: Final = -(2**63)
INT_MAX: Final = 2**64 - 1

_UUID7_RE: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)

Mint = Callable[[], str]


class ProjectionError(ValueError):
    """A document cannot be hydrated or projected; ``code`` is the proposed diagnostics code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------------------------
# Classification (R3)
# --------------------------------------------------------------------------------------------


def is_reference(obj: Any) -> bool:
    """True for a reference: a string ``id`` and no keys beyond ``id``, ``type``, ``_label``."""
    return isinstance(obj, dict) and isinstance(obj.get("id"), str) and obj.keys() <= REFERENCE_KEYS


def is_node(obj: Any) -> bool:
    """True for an embedded node: has ``type``, and ``id`` is absent or a string."""
    return (
        isinstance(obj, dict) and "type" in obj and ("id" not in obj or isinstance(obj["id"], str))
    )


def is_structured(obj: Any) -> bool:
    """True if ``obj`` is stored as a shared ``Map`` (a node or a reference), else it is opaque."""
    return is_reference(obj) or is_node(obj)


def is_internal_node_iri(iri: Any, record_uri: str) -> bool:
    """True if ``iri`` is exactly ``<record_uri>/node/<canonical lower-case UUIDv7>``."""
    prefix = record_uri + "/node/"
    return (
        isinstance(iri, str)
        and iri.startswith(prefix)
        and _UUID7_RE.fullmatch(iri[len(prefix) :]) is not None
    )


def mint_node_iri(record_uri: str) -> str:
    """Mint a fresh internal node IRI for ``record_uri``."""
    return f"{record_uri}/node/{uuid_utils.uuid7()}"


# --------------------------------------------------------------------------------------------
# Plain values (R9, R10)
# --------------------------------------------------------------------------------------------


def _check_text(value: str, where: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProjectionError("LAE-PROJ-003", f"{where} is not valid Unicode: {exc}") from exc
    return value


def _check_numbers(value: Any) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        if not INT_MIN <= value <= INT_MAX:
            raise ProjectionError("LAE-PROJ-002", f"integer {value} is outside the 64-bit range")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ProjectionError("LAE-PROJ-002", f"number {value!r} is not finite")
    elif isinstance(value, dict):
        for item in value.values():
            _check_numbers(item)
    elif isinstance(value, list):
        for item in value:
            _check_numbers(item)


def json_text(value: Any) -> str:
    """Exact JSON text of ``value``: sorted keys, compact, non-ASCII verbatim, no NaN."""
    _check_numbers(value)
    text = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return _check_text(text, "a string")


def encode_plain(value: Any) -> Any:
    """Encode a JSON value that is not a node, reference, or container as a Y plain value (R9)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _check_text(value, "a string")
    if isinstance(value, int):
        _check_numbers(value)
        # Safe integers travel as a Y number (always a double on the wire and in pycrdt).
        return float(value) if abs(value) <= MAX_SAFE_INT else {JSON_KEY: json_text(value)}
    if isinstance(value, float):
        _check_numbers(value)
        # A float with an integral value (33.0, -0.0, 1e21) would come back as an int.
        return {JSON_KEY: json_text(value)} if value.is_integer() else value
    if isinstance(value, dict | list):
        return {JSON_KEY: json_text(value)}
    raise ProjectionError("LAE-PROJ-005", f"not a JSON value: {type(value).__name__}")


def decode_plain(value: Any) -> Any:
    """Decode a Y plain value back to JSON (inverse of ``encode_plain``)."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProjectionError("LAE-PROJ-005", f"non-finite number {value!r} in the document")
        return int(value) if value.is_integer() and abs(value) <= MAX_SAFE_INT else value
    if isinstance(value, dict) and value.keys() == {JSON_KEY} and isinstance(value[JSON_KEY], str):
        return json.loads(value[JSON_KEY])
    raise ProjectionError("LAE-PROJ-005", f"unexpected plain value in the document: {value!r}")


def value_key(value: Any) -> str:
    """Base entry key of an opaque array item (R6)."""
    if isinstance(value, str):
        return "=s:" + value
    if value is None:
        return "=z"
    if isinstance(value, bool):
        return "=b:true" if value else "=b:false"
    if isinstance(value, int | float):
        return "=n:" + json_text(value)
    return "=j:" + json_text(value)


def unique_key(base: str, used: Iterable[str]) -> str:
    """``base`` if unused, else the first unused of ``base #2``, ``base #3``, ... (R7)."""
    taken = used if isinstance(used, set | dict | frozenset) else set(used)
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base} #{n}"
        n += 1
    return candidate


# --------------------------------------------------------------------------------------------
# Hydration
# --------------------------------------------------------------------------------------------


def assign_node_iris(
    jsonld: dict[str, Any], record_uri: str, mint: Mint | None = None
) -> dict[str, Any]:
    """Return a deep copy with ``id`` added to every embedded node that lacks one (R4)."""
    mint_ = mint or (lambda: mint_node_iri(record_uri))
    return _assign_object(jsonld, mint_, is_root=True)


def _assign_object(obj: dict[str, Any], mint: Mint, *, is_root: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in obj.items():
        if key in OPAQUE_KEYS:
            out[key] = value
        elif is_structured(value):
            out[key] = _assign_object(value, mint)
        elif isinstance(value, list):
            out[key] = [_assign_object(i, mint) if is_structured(i) else i for i in value]
        else:
            out[key] = value
    if not is_root and "id" not in out and "type" in out:
        out["id"] = mint()
    return out


def hydrate(
    jsonld: dict[str, Any], record_uri: str | None = None, *, mint: Mint | None = None
) -> Doc:
    """Build a fresh ``Doc`` whose ``record`` map holds ``jsonld`` under the S3 rules."""
    uri = record_uri if record_uri is not None else jsonld.get("id")
    if not isinstance(uri, str) or not uri:
        raise ProjectionError("LAE-PROJ-004", "no record URI: pass record_uri or set a root id")
    data = assign_node_iris(jsonld, uri, mint)
    fields = _object_fields(data)
    doc: Doc = Doc()
    root = doc.get(ROOT_MAP, type=Map)
    with doc.transaction():
        for key, value in fields.items():
            root[key] = value
    return doc


def _object_fields(obj: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in obj.items():
        if not isinstance(key, str):
            raise ProjectionError("LAE-PROJ-005", f"non-string key {key!r}")
        _check_text(key, "a key")
        if key in RESERVED_KEYS:
            raise ProjectionError("LAE-PROJ-001", f"reserved key {key!r} in the input")
        fields[key] = _hydrate_value(key, value, obj)
    return fields


def _hydrate_value(key: str, value: Any, parent: dict[str, Any]) -> Any:
    if key in OPAQUE_KEYS:
        return encode_plain(value)
    if key == "content" and isinstance(value, str):
        parent_type = parent.get("type")
        if isinstance(parent_type, str) and parent_type in TEXT_CONTENT_TYPES:
            return Text(_check_text(value, "content"))
    if is_structured(value):
        return Map(_object_fields(value))
    if isinstance(value, list) and value:
        return _hydrate_array(value)
    return encode_plain(value)


def entry_fields(item: Any) -> tuple[str, dict[str, Any]]:
    """Return ``(base key, Map fields)`` for one array item (ids already assigned)."""
    if is_structured(item):
        return item["id"], _object_fields(item)
    return value_key(item), {VALUE_KEY: encode_plain(item)}


def _hydrate_array(items: list[Any]) -> Map:
    entries: dict[str, Map] = {}
    for index, item in enumerate(items):
        base, fields = entry_fields(item)
        fields[ORDER_KEY] = float(index)
        entries[unique_key(base, entries)] = Map(fields)
    return Map(entries)


# --------------------------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------------------------

_OMIT: Final = object()


def entry_order(entry: Any) -> float | None:
    """The numeric ``_order`` of an entry map, or ``None`` if it has none."""
    if not isinstance(entry, Map) or ORDER_KEY not in entry:
        return None
    order = entry[ORDER_KEY]
    if isinstance(order, bool) or not isinstance(order, int | float) or math.isnan(order):
        return None
    return float(order)


def is_container(value: Any) -> bool:
    """True if a shared map is an array container: every value is an entry map (R11)."""
    return isinstance(value, Map) and all(entry_order(v) is not None for v in value.values())


def sorted_entries(container: Map) -> list[tuple[str, Map]]:
    """Entries by ``_order`` ascending, ties broken by key in Unicode code-point order (R13)."""
    return sorted(container.items(), key=lambda kv: (entry_order(kv[1]), kv[0]))


def project(doc: Doc) -> dict[str, Any]:
    """Project the ``record`` map back to JSON-LD; internal node IRIs are kept."""
    return _project_object(doc.get(ROOT_MAP, type=Map))


def _project_object(obj: Map) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in obj.items():
        if key == ORDER_KEY:
            continue
        projected = _project_value(value)
        if projected is not _OMIT:
            out[key] = projected
    return out


def _project_value(value: Any) -> Any:
    if isinstance(value, Text):
        return str(value)
    if isinstance(value, Map):
        if not is_container(value):
            return _project_object(value)
        if len(value) == 0:
            return _OMIT
        return [_project_entry(entry) for _, entry in sorted_entries(value)]
    return decode_plain(value)


def _project_entry(entry: Map) -> Any:
    if VALUE_KEY in entry:
        return decode_plain(entry[VALUE_KEY])
    return _project_object(entry)


def strip_internal_iris(jsonld: dict[str, Any], record_uri: str | None = None) -> dict[str, Any]:
    """Return a copy without any ``id`` that is an internal node IRI of this record (R15)."""
    uri = record_uri if record_uri is not None else jsonld.get("id")
    if not isinstance(uri, str) or not uri:
        raise ProjectionError("LAE-PROJ-004", "no record URI: pass record_uri or set a root id")
    out = {k: v if k in OPAQUE_KEYS else _strip(v, uri) for k, v in jsonld.items()}
    return out


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


def canonical_json(data: Any) -> str:
    """Canonical JSON text: ``orjson`` with sorted keys."""
    return orjson.dumps(data, option=orjson.OPT_SORT_KEYS).decode()


# --------------------------------------------------------------------------------------------
# _order arithmetic and the minimal ops the spike tests need (R12-R14)
# --------------------------------------------------------------------------------------------


def order_between(before: float | None, after: float | None) -> float | None:
    """The ``_order`` for a slot between two neighbours, or ``None`` if a rebalance is needed."""
    if before is None and after is None:
        return 0.0
    if before is None:
        assert after is not None
        candidate = after - 1.0
        ok = candidate < after
    elif after is None:
        candidate = before + 1.0
        ok = candidate > before
    else:
        candidate = (before + after) / 2.0
        ok = before < candidate < after
    return candidate if ok and math.isfinite(candidate) else None


def rebalance(container: Map) -> None:
    """Renumber a container to 0.0, 1.0, ... in its current projected order."""
    for index, (_, entry) in enumerate(sorted_entries(container)):
        if entry[ORDER_KEY] != float(index):
            entry[ORDER_KEY] = float(index)


def order_for_index(container: Map, index: int, skip: str | None = None) -> float:
    """The ``_order`` that places an entry at ``index``, rebalancing first if needed."""
    for attempt in range(2):
        orders = [entry_order(e) for k, e in sorted_entries(container) if k != skip]
        index = max(0, min(index, len(orders)))
        before = orders[index - 1] if index > 0 else None
        after = orders[index] if index < len(orders) else None
        order = order_between(before, after)
        if order is not None:
            return order
        if attempt == 0:
            rebalance(container)
    raise ProjectionError("LAE-PROJ-005", "no free _order after rebalance")


def insert_entry(
    doc: Doc,
    container: Map,
    item: Any,
    index: int | None = None,
    *,
    record_uri: str,
    mint: Mint | None = None,
) -> str:
    """Insert ``item`` at array position ``index`` (default: append); return its entry key.

    Inserting a reference or opaque value whose key is already present is a no-op (set semantics).
    """
    if is_structured(item):
        item = _assign_object(item, mint or (lambda: mint_node_iri(record_uri)))
    base, fields = entry_fields(item)
    with doc.transaction():
        if base in container:
            return base
        position = len(container) if index is None else index
        fields[ORDER_KEY] = order_for_index(container, position)
        container[base] = Map(fields)
    return base


def move_entry(doc: Doc, container: Map, key: str, index: int) -> None:
    """Move entry ``key`` so that it sits at ``index`` among the other entries."""
    with doc.transaction():
        order = order_for_index(container, index, skip=key)
        container[key][ORDER_KEY] = order


def remove_entry(doc: Doc, container: Map, key: str) -> None:
    """Delete entry ``key`` from a container."""
    with doc.transaction():
        del container[key]
