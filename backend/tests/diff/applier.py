"""Reference applier of semantic-diff changes to a hydrated ``Doc`` (test-only).

``lae.crdt.ops.apply_change`` (Phase 3 Step 3.6) will be the real one. This one exists to prove
the contract of ``lae.diff.semantic``: applying the changes of ``semantic_diff(old, new)`` in
list order to ``hydrate(old)`` gives a document that projects to ``new``.

It goes through the operations of ``lae.projection.ops`` wherever they can express the change,
so the test also shows that the order of the list never asks an operation for something it
rejects (an insert into a property that still holds a single value, a scalar written over an
array). Three things the operations refuse by design are written into the maps directly:

- a property whose key is outside the profile path grammar, or is ``id`` / ``type`` (the root's
  can differ between two arbitrary documents), or a literal that is not a scalar;
- an added array item that repeats one the array already holds (R7: operations never create a
  repeat, a diff towards a document that has one must);
- nothing else.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from pycrdt import Doc, Map, Text

from lae.diff.semantic import (
    AddNode,
    AddRef,
    RemoveNode,
    RemoveRef,
    Reorder,
    Step,
    UpdateLiteral,
)
from lae.projection import ops
from lae.projection.hydrate import entry_fields, property_value
from lae.projection.project import record_map, sorted_entries
from lae.projection.rules import (
    IDENTITY_KEYS,
    ORDER_KEY,
    entry_key,
    is_structured,
    parse_key,
    property_key,
    unique_key,
    value_key,
)

_SIMPLE_KEY = re.compile(r"[a-z][a-z0-9_]*")

Applied = AddNode | AddRef | RemoveNode | RemoveRef | UpdateLiteral | Reorder


def _path_ok(prop: str) -> bool:
    """Whether ``prop`` alone is a path the write operations accept."""
    return _SIMPLE_KEY.fullmatch(prop) is not None and prop not in IDENTITY_KEYS


def locate(doc: Doc[Any], parent: list[Step]) -> Map[Any]:
    """The object map the steps lead to: entry keys for array items, property keys otherwise."""
    node = record_map(doc)
    for step in parent:
        key = (
            property_key(step.property) if step.key is None else entry_key(step.property, step.key)
        )
        node = node[key]
        assert isinstance(node, Map), f"{step} does not lead to an object"
    return node


def _insert(
    doc: Doc[Any], parent: Map[Any], prop: str, key: str, before: str | None, item: Any
) -> None:
    existing = dict(sorted_entries(parent, prop))
    wanted = entry_key(prop, key)
    assert wanted not in existing, f"{wanted} is already there"
    index = None if before is None else list(existing).index(entry_key(prop, before))
    base = item["id"] if is_structured(item) else value_key(item)
    used = {parse_key(k)[1] or "" for k in existing}
    repeat = any(ops._same_item(entry, item) for entry in existing.values())
    if not repeat and entry_key(prop, unique_key(base, used)) == wanted:
        assert ops.insert_item(doc, parent, prop, item, index) == wanted
        return
    # A repeat, which the operations never create (R7): write the entry as hydration would.
    with doc.transaction():
        fields = entry_fields(item)[1]
        position = len(existing) if index is None else index
        fields[ORDER_KEY] = ops.order_for_index(parent, prop, position)
        parent[wanted] = Map(fields)


def _set_single(doc: Doc[Any], parent: Map[Any], prop: str, value: Any) -> None:
    assert not sorted_entries(parent, prop), f"{prop} still has entries"
    if _path_ok(prop) and is_structured(value):
        ops.set_object(doc, prop, value, base=parent)
    elif not isinstance(value, dict | list) and _path_ok(prop):
        ops.set_scalar(doc, prop, value, base=parent)
    else:
        with doc.transaction():
            parent[property_key(prop)] = property_value(prop, value, parent.get("type"))


def _clear_single(doc: Doc[Any], parent: Map[Any], prop: str) -> None:
    if _path_ok(prop):
        assert ops.remove_value(doc, prop, base=parent)
    else:
        with doc.transaction():
            del parent[property_key(prop)]


def apply_change(doc: Doc[Any], change: Applied) -> None:
    """Apply one change to ``doc``."""
    parent = locate(doc, change.parent)
    prop = change.property
    # Below the root an object keeps its identity: a changed id or type is a replacement.
    assert prop not in IDENTITY_KEYS or not change.parent, f"{prop} written on a nested object"
    if isinstance(change, Reorder):
        for index, key in enumerate(change.order):
            ops.move_item(doc, parent, entry_key(prop, key), index)
        return
    if isinstance(change, RemoveNode | RemoveRef) or (
        isinstance(change, UpdateLiteral) and change.op == "clear"
    ):
        if change.key is None:
            _clear_single(doc, parent, prop)
        else:
            assert entry_key(prop, change.key) in parent
            ops.remove_item(doc, parent, entry_key(prop, change.key))
        return
    if isinstance(change, UpdateLiteral):
        if change.text_edit is not None:
            text = parent[prop]
            assert isinstance(text, Text) and str(text) == change.old
            edit = change.text_edit
            ops.splice_text(doc, prop, edit.index, edit.delete, edit.insert, base=parent)
            assert str(text) == change.new
            return
        value = change.new
    else:
        value = change.value
    if change.key is None:
        _set_single(doc, parent, prop, value)
    else:
        _insert(doc, parent, prop, change.key, change.before, value)


def apply_changes(doc: Doc[Any], changes: Iterable[Applied]) -> None:
    """Apply ``changes`` in order."""
    for change in changes:
        apply_change(doc, change)
