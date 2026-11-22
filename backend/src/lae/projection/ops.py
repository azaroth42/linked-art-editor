"""Operations on a hydrated record ``Doc``: the write path of the API, agents and the editor.

Paths use the UI Profile path syntax of ``profiles/README.md``. The rules are R6, R7, R11-R14
and R19-R25 of ``docs/spikes/S3-projection.md``. ``frontend/src/crdt/paths.ts`` (Step 1.17)
mirrors this module, and both must leave the same document behind for the same calls: the
shared cases are ``backend/tests/projection/fixtures/*.json``.

Conventions for every function that takes a path:

- ``base`` is the object map the path starts from; the default is the root ``record`` map.
  One entry of an array is reached by passing its map (from ``get_entry``, ``list_entries`` or
  ``get_path``) as ``base``, never by encoding its key in the path.
- A write follows a single chain of nodes (R20). A missing intermediate node is created (R21);
  functions that only read or remove never create anything. A path that is written through
  never names ``id`` or ``type`` (``LAE-PROJ-007``); ``get_path``, ``list_entries`` and
  ``get_entry`` may read them.
- Every write is one transaction and is checked before it changes anything, so a rejected
  call leaves the document as it was. Callers that need an origin wrap the call in their own
  ``doc.transaction(origin=...)``.
- ``mint`` replaces ``lae.ids.node_iri`` for the internal node IRIs an operation creates;
  ``record_uri`` defaults to the root ``id`` and is only needed when something is minted.
- An entry is named by its **entry key** (the map key, ``rules.entry_key(property, item key)``),
  which ``add_node`` and ``add_reference`` return.
"""

from __future__ import annotations

from typing import Any

from pycrdt import Doc, Map, Text

from lae.ids import node_iri
from lae.profiles.paths import PathSegment, parse_path
from lae.projection.hydrate import (
    Mint,
    assign_object,
    entry_fields,
    is_text_content,
    object_fields,
    resolve_record_uri,
)
from lae.projection.project import (
    entry_order,
    project_object,
    record_map,
    sorted_entries,
)
from lae.projection.rules import (
    EMPTY_ARRAY,
    IDENTITY_KEYS,
    OPAQUE_KEYS,
    ORDER_KEY,
    PROJ_BAD_PATH,
    PROJ_BAD_STATE,
    PROJ_NO_ENTRY,
    PROJ_RESERVED_KEY,
    PROJ_SINGLE_VALUE,
    RESERVED_KEYS,
    SINGLE_VALUED_CLASSES,
    VALUE_KEY,
    ProjectionError,
    check_json,
    check_text,
    decode_plain,
    encode_plain,
    entry_key,
    is_node,
    is_structured,
    order_between,
    parse_key,
    property_key,
    text_change,
    unique_key,
    utf8_offset,
    value_key,
)

# --------------------------------------------------------------------------------------------
# Entries of one property of one object map (R12-R14)
# --------------------------------------------------------------------------------------------


def rebalance(parent: Map[Any], prop: str) -> None:
    """Renumber the entries of one property to 0.0, 1.0, ... in their projected order (R12)."""
    for index, (_, entry) in enumerate(sorted_entries(parent, prop)):
        if entry[ORDER_KEY] != float(index):
            entry[ORDER_KEY] = float(index)


def order_for_index(parent: Map[Any], prop: str, index: int, skip: str | None = None) -> float:
    """The ``_order`` that places an entry at ``index``, rebalancing first if needed (R12).

    ``skip`` is the key of an entry to leave out when finding the neighbours (the entry being
    moved). Must be called inside the transaction that then writes the result.
    """
    for attempt in range(2):
        orders = [entry_order(e) for k, e in sorted_entries(parent, prop) if k != skip]
        index = max(0, min(index, len(orders)))
        before = orders[index - 1] if index > 0 else None
        after = orders[index] if index < len(orders) else None
        order = order_between(before, after)
        if order is not None:
            return order
        if attempt == 0:
            rebalance(parent, prop)
    raise ProjectionError(PROJ_BAD_STATE, "no free _order after rebalance")


def _same_item(entry: Map[Any], item: Any) -> bool:
    if is_structured(item):
        return VALUE_KEY not in entry and entry.get("id") == item["id"]
    return VALUE_KEY in entry and value_key(decode_plain(entry[VALUE_KEY])) == value_key(item)


def _find_item(existing: dict[str, Map[Any]], prop: str, item: Any) -> str | None:
    """The key of the entry of ``prop`` that already holds ``item``, if any (R7).

    The entry under the item's base item key if that is the same item, else the first entry in
    projected order that is (a suffixed duplicate left by an import).
    """
    if not is_structured(item):
        base = value_key(item)
    elif "id" in item:
        base = item["id"]
    else:
        return None  # a node that has no id yet is new
    key = entry_key(prop, base)
    if key in existing and _same_item(existing[key], item):
        return key
    return next((k for k, entry in existing.items() if _same_item(entry, item)), None)


def _default_mint(doc: Doc[Any], record_uri: str | None, mint: Mint | None) -> Mint:
    if mint is not None:
        return mint

    def mint_from_root() -> str:
        return node_iri(resolve_record_uri(record_uri, record_map(doc).get("id")))

    return mint_from_root


def _validate_object(item: dict[str, Any]) -> None:
    """Raise what storing ``item`` would raise (R10), without minting anything."""
    object_fields(assign_object(item, lambda: ""))


def _prepare_item(item: Any, mint: Mint) -> tuple[Any, str, dict[str, Any]]:
    """Validate ``item`` and return it with ids assigned, its base item key and its fields."""
    if is_structured(item):
        item = assign_object(item, mint)
    else:
        check_json(item)
    base, fields = entry_fields(item)
    return item, base, fields


def _insert_prepared(
    parent: Map[Any],
    prop: str,
    item: Any,
    base: str,
    fields: dict[str, Any],
    index: int | None,
) -> str:
    """Write one entry (R7, R12, R14); must run inside a transaction."""
    existing = dict(sorted_entries(parent, prop))
    present = _find_item(existing, prop, item)
    if present is not None:
        return present
    key = entry_key(prop, base)
    if key in existing:  # the base item key is held by a suffixed duplicate of another item
        used = {parse_key(k)[1] or "" for k in existing}
        key = entry_key(prop, unique_key(base, used))
    single = property_key(prop)
    if single in parent:
        # Allowed to go: the marker of an empty source array, and a single value that
        # concurrent edits left behind entries (the projection already ignores it, R11).
        if parent[single] != EMPTY_ARRAY and not existing:
            raise ProjectionError(PROJ_SINGLE_VALUE, f"{prop!r} holds a single value")
        del parent[single]
    position = len(existing) if index is None else index
    fields[ORDER_KEY] = order_for_index(parent, prop, position)
    parent[key] = Map(fields)
    return key


def insert_item(
    doc: Doc[Any],
    parent: Map[Any],
    prop: str,
    item: Any,
    index: int | None = None,
    *,
    record_uri: str | None = None,
    mint: Mint | None = None,
) -> str:
    """Insert ``item`` at position ``index`` (default: append) of ``parent[prop]``; return its key.

    The primitive under ``add_node`` and ``add_reference``, for callers that hold the parent
    map. ``item`` is any JSON value: a node (embedded nodes without ``id`` get one), a
    reference, or an opaque value. No container is created: the entry is one more key of
    ``parent`` (R14). Inserting an item that the array already holds under its base item key
    (same ``id``, or an equal opaque value) is a no-op that returns the existing key (R7).
    """
    if prop in RESERVED_KEYS or prop in OPAQUE_KEYS:
        raise ProjectionError(PROJ_RESERVED_KEY, f"{prop!r} cannot be an array property")
    item, base, fields = _prepare_item(item, _default_mint(doc, record_uri, mint))
    with doc.transaction():
        return _insert_prepared(parent, prop, item, base, fields, index)


def move_item(doc: Doc[Any], parent: Map[Any], key: str, index: int) -> None:
    """Move entry ``key`` so that it sits at ``index`` among the other entries of its property."""
    prop = parse_key(key)[0]
    with doc.transaction():
        if key not in parent:
            raise ProjectionError(PROJ_NO_ENTRY, f"no entry {key!r}")
        order = order_for_index(parent, prop, index, skip=key)
        parent[key][ORDER_KEY] = order


def remove_item(doc: Doc[Any], parent: Map[Any], key: str) -> None:
    """Delete entry ``key`` of ``parent``; nothing else is touched (R12)."""
    with doc.transaction():
        del parent[key]


# --------------------------------------------------------------------------------------------
# Reading a path (profiles/README.md, "Reading")
# --------------------------------------------------------------------------------------------


def _step(value: Any, key: str) -> list[Any]:
    """What ``key`` holds in ``value``: each array item in order, or the single value."""
    if isinstance(value, Map):
        entries = sorted_entries(value, key)
        if entries:  # entries win over a single value (R11)
            return [
                decode_plain(entry[VALUE_KEY]) if VALUE_KEY in entry else entry
                for _, entry in entries
            ]
        if key not in value:
            return []
        stored = value[key]
        if isinstance(stored, Map | Text):
            return [stored]
        decoded = decode_plain(stored)
        return list(decoded) if isinstance(decoded, list) else [decoded]
    if isinstance(value, dict):  # inside an opaque value: plain JSON
        if key not in value:
            return []
        held = value[key]
        return list(held) if isinstance(held, list) else [held]
    return []


def _has_type(value: Any, cls: str) -> bool:
    """True if ``value`` is an object whose ``type`` is exactly the string ``cls``."""
    if isinstance(value, Map):
        return VALUE_KEY not in value and value.get("type") == cls
    return isinstance(value, dict) and value.get("type") == cls


def _select(value: Any, segment: PathSegment) -> list[Any]:
    found = _step(value, segment.key)
    if segment.type_filter is None:
        return found
    return [v for v in found if _has_type(v, segment.type_filter)]


def get_path(doc: Doc[Any], path: str, *, base: Map[Any] | None = None) -> list[Any]:
    """Every value ``path`` addresses, in document order.

    Nodes and references come back as their live object maps, ``Text`` values as ``Text``,
    everything else as decoded JSON; ``value_to_json`` turns any of them into JSON. The result
    equals evaluating the path against ``project(doc)``.
    """
    current: list[Any] = [record_map(doc) if base is None else base]
    for segment in parse_path(path):
        current = [found for value in current for found in _select(value, segment)]
    return current


def value_to_json(value: Any) -> Any:
    """The JSON form of one value returned by ``get_path``."""
    if isinstance(value, Map):
        return project_object(value)
    if isinstance(value, Text):
        return str(value)
    return value


# --------------------------------------------------------------------------------------------
# Following a write path (R20, R21)
# --------------------------------------------------------------------------------------------


def _write_path(path: str) -> tuple[PathSegment, ...]:
    """Parse the path of an operation that writes; it may not name ``id`` or ``type`` (R20).

    Checked before anything else: every node a path creates already holds both keys, so a
    write through either would overwrite a node's identity or fail after creating the node.
    """
    segments = parse_path(path)
    for segment in segments:
        if segment.key in IDENTITY_KEYS:
            raise ProjectionError(PROJ_BAD_PATH, f"a write path cannot name {segment.key!r}")
    return segments


def _locate(
    doc: Doc[Any], base: Map[Any] | None, segments: tuple[PathSegment, ...], *, create: bool
) -> tuple[Map[Any], tuple[PathSegment, ...]]:
    """Follow ``segments`` (the intermediate steps of a write path) as far as nodes exist.

    Returns the deepest existing node and the segments that are missing below it. With
    ``create`` the missing segments are checked to be creatable (R21), so that
    ``_materialize`` cannot fail after it has started writing.
    """
    node = record_map(doc) if base is None else base
    for position, segment in enumerate(segments):
        found = _select(node, segment)
        if len(found) > 1:
            raise ProjectionError(PROJ_BAD_PATH, f"{segment} addresses {len(found)} values")
        if not found:
            missing = segments[position:]
            if create:
                _check_creatable(node, missing)
            return node, missing
        if not isinstance(found[0], Map):
            raise ProjectionError(PROJ_BAD_PATH, f"{segment} does not address a node")
        node = found[0]
    return node, ()


def _check_creatable(node: Map[Any], missing: tuple[PathSegment, ...]) -> None:
    for segment in missing:
        if segment.key not in SINGLE_VALUED_CLASSES and segment.type_filter is None:
            raise ProjectionError(
                PROJ_BAD_PATH, f"cannot create {segment}: add a class filter to the path"
            )
    first = missing[0]
    has_entries = bool(sorted_entries(node, first.key))
    if first.key in SINGLE_VALUED_CLASSES:
        # The property holds something the segment does not address; never overwrite it.
        if first.key in node or has_entries:
            raise ProjectionError(PROJ_BAD_PATH, f"{first.key!r} holds another value")
    elif first.key in node and node[first.key] != EMPTY_ARRAY and not has_entries:
        raise ProjectionError(PROJ_SINGLE_VALUE, f"{first.key!r} holds a single value")


def _materialize(node: Map[Any], missing: tuple[PathSegment, ...], mint: Mint) -> Map[Any]:
    """Create the missing intermediate nodes below ``node`` (R21); must run in a transaction."""
    for segment in missing:
        single_class = SINGLE_VALUED_CLASSES.get(segment.key)
        created = {"id": mint(), "type": segment.type_filter or single_class}
        if single_class is not None:
            node[segment.key] = Map(object_fields(created))
            node = node[segment.key]
        else:
            _, base_key, fields = _prepare_item(created, mint)
            node = node[_insert_prepared(node, segment.key, created, base_key, fields, None)]
    return node


def _existing_parent(
    doc: Doc[Any], base: Map[Any] | None, segments: tuple[PathSegment, ...]
) -> Map[Any] | None:
    """The node that holds the last segment's property, or ``None`` if the chain is incomplete."""
    node, missing = _locate(doc, base, segments[:-1], create=False)
    return None if missing else node


def _entry_prop(key: str, prop: str) -> None:
    key_prop, item_key = parse_key(key)
    if item_key is None or key_prop != prop:
        raise ProjectionError(PROJ_NO_ENTRY, f"{key!r} is not an entry key of {prop!r}")


def _with_type(obj: Any, segment: PathSegment) -> dict[str, Any]:
    """A copy of ``obj`` whose ``type`` is the segment's class filter when it has none (R22)."""
    if not isinstance(obj, dict):
        raise ProjectionError(PROJ_BAD_PATH, "the value must be a JSON object")
    out = dict(obj)
    if segment.type_filter is not None:
        if out.setdefault("type", segment.type_filter) != segment.type_filter:
            raise ProjectionError(PROJ_BAD_PATH, f"type {out['type']!r} does not match {segment}")
    return out


# --------------------------------------------------------------------------------------------
# Entries addressed by a path
# --------------------------------------------------------------------------------------------


def list_entries(
    doc: Doc[Any], path: str, *, base: Map[Any] | None = None
) -> list[tuple[str, Map[Any]]]:
    """The array entries the last segment of ``path`` addresses, as ``(entry key, entry map)``.

    In projected order, restricted to the segment's class filter if it has one. An entry whose
    item is opaque is a map with the single item under ``_value``. Empty if the chain of nodes
    before the last segment does not exist.
    """
    segments = parse_path(path)
    last = segments[-1]
    parent = _existing_parent(doc, base, segments)
    if parent is None:
        return []
    entries = sorted_entries(parent, last.key)
    if last.type_filter is None:
        return entries
    return [(key, entry) for key, entry in entries if _has_type(entry, last.type_filter)]


def get_entry(doc: Doc[Any], path: str, key: str, *, base: Map[Any] | None = None) -> Map[Any]:
    """The entry map with entry key ``key`` of the array ``path`` addresses (``LAE-PROJ-008``)."""
    segments = parse_path(path)
    _entry_prop(key, segments[-1].key)
    parent = _existing_parent(doc, base, segments)
    if parent is None or key not in parent:
        raise ProjectionError(PROJ_NO_ENTRY, f"no entry {key!r}")
    entry = parent[key]
    if not isinstance(entry, Map):
        raise ProjectionError(PROJ_BAD_STATE, f"entry {key!r} is not a map")
    return entry


def _add(
    doc: Doc[Any],
    segments: tuple[PathSegment, ...],
    item: dict[str, Any],
    before_key: str | None,
    base: Map[Any] | None,
    mint: Mint,
) -> str:
    prop = segments[-1].key
    if before_key is not None:
        _entry_prop(before_key, prop)
    _validate_object(item)
    node, missing = _locate(doc, base, segments[:-1], create=True)
    index: int | None = None
    if not missing:
        existing = dict(sorted_entries(node, prop))
        present = _find_item(existing, prop, item)
        if present is not None:
            return present  # already there (R7): nothing is minted or written
        if prop in node and node[prop] != EMPTY_ARRAY and not existing:
            raise ProjectionError(PROJ_SINGLE_VALUE, f"{prop!r} holds a single value")
    if before_key is not None:
        keys = [] if missing else list(existing)
        if before_key not in keys:
            raise ProjectionError(PROJ_NO_ENTRY, f"no entry {before_key!r}")
        index = keys.index(before_key)
    item, base_key, fields = _prepare_item(item, mint)
    with doc.transaction():
        parent = _materialize(node, missing, mint)
        return _insert_prepared(parent, prop, item, base_key, fields, index)


def add_node(
    doc: Doc[Any],
    path: str,
    node: dict[str, Any],
    *,
    before_key: str | None = None,
    base: Map[Any] | None = None,
    record_uri: str | None = None,
    mint: Mint | None = None,
) -> str:
    """Add an embedded node to the array ``path`` addresses and return its entry key.

    The node, and every node inside it, gets an internal node IRI unless it already has an
    ``id`` (R4). It is placed after the last entry, or just before the entry ``before_key``
    (R12). A node without ``type`` takes the class filter of the last segment (R22). A node
    whose ``id`` the array already holds is not added again (R7).
    """
    segments = _write_path(path)
    item = _with_type(node, segments[-1])
    if not is_node(item):
        raise ProjectionError(PROJ_BAD_PATH, "a node needs a type and, if it has an id, a string")
    return _add(doc, segments, item, before_key, base, _default_mint(doc, record_uri, mint))


def add_reference(
    doc: Doc[Any],
    path: str,
    uri: str,
    type: str | None = None,
    *,
    label: str | None = None,
    before_key: str | None = None,
    base: Map[Any] | None = None,
    record_uri: str | None = None,
    mint: Mint | None = None,
) -> str:
    """Add the reference ``{"id": uri, "type": type}`` to the array ``path`` addresses.

    Returns its entry key. If the array already holds a reference to ``uri`` nothing changes
    and that entry's key is returned (R7), which is what makes two clients adding the same
    reference converge on one entry. ``type`` defaults to the class filter of the last segment;
    ``label`` is stored as ``_label``.
    """
    segments = _write_path(path)
    if not isinstance(uri, str):
        raise ProjectionError(PROJ_BAD_PATH, "a reference needs a string id")
    reference: dict[str, Any] = {"id": uri}
    if type is not None:
        reference["type"] = type
    if label is not None:
        reference["_label"] = label
    item = _with_type(reference, segments[-1])
    return _add(doc, segments, item, before_key, base, _default_mint(doc, record_uri, mint))


def remove_entry(doc: Doc[Any], path: str, key: str, *, base: Map[Any] | None = None) -> bool:
    """Delete the entry with entry key ``key`` from the array ``path`` addresses.

    Returns False, changing nothing, if there is no such entry (it may have been removed by
    someone else). Nothing but the one key is touched; removing the last entry leaves the
    property absent (R14).
    """
    segments = _write_path(path)
    _entry_prop(key, segments[-1].key)
    parent = _existing_parent(doc, base, segments)
    if parent is None or key not in parent:
        return False
    remove_item(doc, parent, key)
    return True


def move_entry(
    doc: Doc[Any], path: str, key: str, before_key: str | None, *, base: Map[Any] | None = None
) -> None:
    """Move entry ``key`` to just before entry ``before_key``, or to the end if it is ``None``.

    Both are entry keys of the property named by the last segment; a class filter there does
    not restrict the neighbours. Only the moved entry's ``_order`` is written, unless the gap
    is exhausted or tied and the property is rebalanced first (R12). Nothing is written if the
    entry is already in that position.
    """
    segments = _write_path(path)
    prop = segments[-1].key
    _entry_prop(key, prop)
    if before_key is not None:
        _entry_prop(before_key, prop)
    parent = _existing_parent(doc, base, segments)
    keys = [] if parent is None else [k for k, _ in sorted_entries(parent, prop)]
    if parent is None or key not in keys:
        raise ProjectionError(PROJ_NO_ENTRY, f"no entry {key!r}")
    if before_key == key:
        return
    others = [k for k in keys if k != key]
    if before_key is None:
        index = len(others)
    elif before_key in others:
        index = others.index(before_key)
    else:
        raise ProjectionError(PROJ_NO_ENTRY, f"no entry {before_key!r}")
    if keys.index(key) != index:
        move_item(doc, parent, key, index)


# --------------------------------------------------------------------------------------------
# Single values
# --------------------------------------------------------------------------------------------


def _splice(text: Text, index: int, delete: int, insert: str) -> None:
    """Replace ``delete`` code points at code-point position ``index`` with ``insert`` (R19)."""
    current = str(text)
    if index < 0 or delete < 0 or index + delete > len(current):
        raise ProjectionError(PROJ_NO_ENTRY, f"no text range {index}+{delete} in {len(current)}")
    start = utf8_offset(current, index)
    if delete:
        del text[start : utf8_offset(current, index + delete)]
    if insert:
        text.insert(start, insert)


def set_scalar(
    doc: Doc[Any],
    path: str,
    value: str | int | float | bool | None,
    *,
    base: Map[Any] | None = None,
    record_uri: str | None = None,
    mint: Mint | None = None,
) -> None:
    """Set the single-valued property ``path`` addresses to a string, number, boolean or null.

    ``content`` of a ``Name``, ``Identifier`` or ``LinguisticObject`` is a ``Text`` (R8.2): an
    existing one is edited with the one splice that turns the old string into the new one, so
    that concurrent edits to other parts of the text survive (R23). Every other value is a
    last-writer-wins register and is always written. Rejected with ``LAE-PROJ-007`` if the
    property has array entries or holds a node or reference.
    """
    segments = _write_path(path)
    last = segments[-1]
    if last.type_filter is not None:
        raise ProjectionError(PROJ_BAD_PATH, f"{last}: a scalar has no class")
    if isinstance(value, dict | list):
        raise ProjectionError(PROJ_BAD_PATH, "not a scalar")
    encoded = encode_plain(value)
    node, missing = _locate(doc, base, segments[:-1], create=True)
    if not missing:
        if sorted_entries(node, last.key):
            raise ProjectionError(PROJ_BAD_PATH, f"{last.key!r} is an array")
        if isinstance(node.get(last.key), Map):
            raise ProjectionError(PROJ_BAD_PATH, f"{last.key!r} holds a node or reference")
    with doc.transaction():
        parent = _materialize(node, missing, _default_mint(doc, record_uri, mint))
        if is_text_content(last.key, value, parent.get("type")):
            assert isinstance(value, str)
            existing = parent.get(last.key)
            if isinstance(existing, Text):
                index, delete, insert = text_change(str(existing), value)
                _splice(existing, index, delete, insert)
            else:
                parent[last.key] = Text(value)
        else:
            parent[last.key] = encoded


def splice_text(
    doc: Doc[Any],
    path: str,
    index: int,
    delete: int,
    insert: str,
    *,
    base: Map[Any] | None = None,
) -> None:
    """Edit the ``Text`` at ``path``: delete ``delete`` code points at ``index``, then insert.

    ``index`` and ``delete`` count Unicode code points (R19). ``LAE-PROJ-007`` if the path does
    not address a ``Text`` or an argument has the wrong type; ``LAE-PROJ-008`` if the range is
    outside the text.
    """
    segments = _write_path(path)
    last = segments[-1]
    if not isinstance(insert, str):
        raise ProjectionError(PROJ_BAD_PATH, "the text to insert must be a string")
    for number in (index, delete):
        if isinstance(number, bool) or not isinstance(number, int):
            raise ProjectionError(PROJ_BAD_PATH, "index and delete must be integers")
    check_text(insert, "the text to insert")
    parent = _existing_parent(doc, base, segments)
    text = None if parent is None or last.type_filter is not None else parent.get(last.key)
    if not isinstance(text, Text) or (parent is not None and sorted_entries(parent, last.key)):
        raise ProjectionError(PROJ_BAD_PATH, f"{path!r} does not address a Text")
    with doc.transaction():
        _splice(text, index, delete, insert)


def set_object(
    doc: Doc[Any],
    path: str,
    obj: dict[str, Any],
    *,
    base: Map[Any] | None = None,
    record_uri: str | None = None,
    mint: Mint | None = None,
) -> str:
    """Set the single-valued property ``path`` addresses to a node or reference; return its id.

    The whole value is replaced (a single-valued property is a last-writer-wins register, R14),
    which is also how a single reference is pointed at another target (R5). Nodes without
    ``id`` get an internal node IRI; an object without ``type`` takes the last segment's class
    filter. Rejected with ``LAE-PROJ-007`` if the property has array entries.
    """
    segments = _write_path(path)
    last = segments[-1]
    item = _with_type(obj, last)
    if not is_structured(item):
        raise ProjectionError(PROJ_BAD_PATH, "not a node or reference")
    _validate_object(item)
    node, missing = _locate(doc, base, segments[:-1], create=True)
    if not missing and sorted_entries(node, last.key):
        raise ProjectionError(PROJ_BAD_PATH, f"{last.key!r} is an array")
    mint_ = _default_mint(doc, record_uri, mint)
    item = assign_object(item, mint_)
    fields = object_fields(item)
    with doc.transaction():
        parent = _materialize(node, missing, mint_)
        parent[last.key] = Map(fields)
    item_id: str = item["id"]
    return item_id


def remove_value(doc: Doc[Any], path: str, *, base: Map[Any] | None = None) -> bool:
    """Delete the single value ``path`` addresses (a scalar, ``Text``, node or reference).

    Returns False, changing nothing, if the property is absent or, when the last segment has a
    class filter, its value is not an object of that class. Rejected with ``LAE-PROJ-007`` if
    the property has array entries: those are removed one by one with ``remove_entry``.
    """
    segments = _write_path(path)
    last = segments[-1]
    parent = _existing_parent(doc, base, segments)
    if parent is None:
        return False
    if sorted_entries(parent, last.key):
        raise ProjectionError(PROJ_BAD_PATH, f"{last.key!r} is an array")
    if last.key not in parent:
        return False
    if last.type_filter is not None and not _has_type(parent[last.key], last.type_filter):
        return False
    with doc.transaction():
        del parent[last.key]
    return True
