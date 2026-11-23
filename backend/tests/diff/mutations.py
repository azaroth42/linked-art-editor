"""Random edits of a projected record, for the semantic-diff property test.

``mutate(record, rng, donor)`` returns an edited deep copy of ``record``. Each edit is something
an editor, an import or a merge can do to a record: change a string, add, remove, move, repeat
or reorder array items, turn a single value into an array and back, change a node's type, graft
a property from another record (``donor``). Every embedded node of the result has an ``id``.
"""

from __future__ import annotations

import copy
import random
from collections.abc import Callable, Iterator
from typing import Any

from lae.ids import node_iri
from lae.projection import assign_node_iris, canonical_json
from lae.projection.rules import OPAQUE_KEYS, is_structured

AAT = "http://vocab.getty.edu/aat/"
URIS = [f"{AAT}300404670", f"{AAT}300404671", f"{AAT}300015045", "https://example.org/thing #2"]
CHARS = ["a", "é", "é", " ", "\U0001f600", "中", "x #2", ""]
KEYS = ["classified_as", "identified_by", "referred_to_by", "member_of", "x_new", "note"]

Mutation = Callable[[dict[str, Any], random.Random, dict[str, Any]], None]


def objects(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The root and every node or reference below it (not inside opaque values)."""
    return list(_objects(record))


def _objects(obj: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield obj
    for key, value in obj.items():
        if key in OPAQUE_KEYS:
            continue
        for item in value if isinstance(value, list) else [value]:
            if is_structured(item):
                yield from _objects(item)


def arrays(record: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """``(object, key)`` of every non-empty array property."""
    return [
        (obj, key)
        for obj in objects(record)
        for key, value in obj.items()
        if key not in OPAQUE_KEYS and isinstance(value, list) and value
    ]


def _new_node(record: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_iri(record["id"]),
        "type": rng.choice(["Name", "Identifier", "LinguisticObject", "Dimension"]),
        "content": "".join(rng.choices(CHARS, k=rng.randint(0, 6))),
    }
    if rng.random() < 0.6:
        node["classified_as"] = [{"id": rng.choice(URIS), "type": "Type", "_label": "Kind"}]
    if rng.random() < 0.3:
        node["value"] = rng.choice([1, 1.0, 33.5, -0.0])
    return node


def _new_ref(rng: random.Random) -> dict[str, Any]:
    ref: dict[str, Any] = {"id": rng.choice(URIS)}
    if rng.random() < 0.8:
        ref["type"] = rng.choice(["Type", "Language"])
    if rng.random() < 0.3:
        ref["_label"] = rng.choice(CHARS)
    return ref


def edit_string(record: dict[str, Any], rng: random.Random, donor: dict[str, Any]) -> None:
    """Splice a string anywhere: a ``content``, a date, a ``_label``, an ``id``, a ``type``."""
    slots = [
        (obj, key)
        for obj in objects(record)
        for key, value in obj.items()
        if isinstance(value, str) and not (obj is record and key == "id")
    ]
    if not slots:
        return
    obj, key = rng.choice(slots)
    text: str = obj[key]
    start = rng.randint(0, len(text))
    end = rng.randint(start, min(len(text), start + 3))
    obj[key] = text[:start] + "".join(rng.choices(CHARS, k=rng.randint(0, 2))) + text[end:]


def delete_key(record: dict[str, Any], rng: random.Random, donor: dict[str, Any]) -> None:
    obj = rng.choice(objects(record))
    keys = [k for k in obj if not (obj is record and k == "id")]
    if keys:
        del obj[rng.choice(keys)]


def remove_item(record: dict[str, Any], rng: random.Random, donor: dict[str, Any]) -> None:
    found = arrays(record)
    if found:
        obj, key = rng.choice(found)
        obj[key].pop(rng.randrange(len(obj[key])))
        if not obj[key] and rng.random() < 0.7:
            del obj[key]


def reorderable(record: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """``(object, key)`` of every array that holds two different items: one ``reorder`` can
    change."""
    return [
        (obj, key)
        for obj, key in arrays(record)
        if len({canonical_json(item) for item in obj[key]}) > 1
    ]


def reorder(record: dict[str, Any], rng: random.Random, donor: dict[str, Any]) -> None:
    """Change the order of an array that holds two different items (shuffle, or move one).

    The order always changes when some array can change: a shuffle or a move that happens to
    give the same order back is followed by a swap of two neighbours that differ. Only records
    without a ``reorderable`` array are left as they are.
    """
    found = reorderable(record)
    if not found:
        return
    obj, key = rng.choice(found)
    items: list[Any] = obj[key]
    original = canonical_json(items)
    if rng.random() < 0.5:
        rng.shuffle(items)
    else:
        item = items.pop(rng.randrange(len(items)))
        items.insert(rng.randint(0, len(items)), item)
    if canonical_json(items) == original:
        index = next(
            i
            for i in range(len(items) - 1)
            if canonical_json(items[i]) != canonical_json(items[i + 1])
        )
        items[index], items[index + 1] = items[index + 1], items[index]


def add_item(record: dict[str, Any], rng: random.Random, donor: dict[str, Any]) -> None:
    """Add a reference, a node or an opaque item to an existing or a new array."""
    obj = rng.choice(objects(record))
    existing = [k for k, v in obj.items() if isinstance(v, list) and k not in OPAQUE_KEYS]
    key = rng.choice(existing) if existing and rng.random() < 0.7 else rng.choice(KEYS)
    current = obj.get(key)
    if not isinstance(current, list):
        current = obj[key] = []
    kind = rng.random()
    if kind < 0.45:
        item: Any = _new_ref(rng)
    elif kind < 0.9:
        item = _new_node(record, rng)
    else:
        item = rng.choice(["x", "x #2", 1, 1.0, True, None, {"@value": "v"}, [1, 2]])
    current.insert(rng.randint(0, len(current)), item)


def repeat_item(record: dict[str, Any], rng: random.Random, donor: dict[str, Any]) -> None:
    """Repeat an array item (same ``id``, so the copy is a `` #n`` entry)."""
    found = arrays(record)
    if found:
        obj, key = rng.choice(found)
        item = copy.deepcopy(rng.choice(obj[key]))
        if isinstance(item, dict) and rng.random() < 0.5:
            item["_label"] = "copy"
        obj[key].insert(rng.randint(0, len(obj[key])), item)


def move_item(record: dict[str, Any], rng: random.Random, donor: dict[str, Any]) -> None:
    """Move an array item to another array, possibly of another object."""
    found = arrays(record)
    if not found:
        return
    obj, key = rng.choice(found)
    item = obj[key].pop(rng.randrange(len(obj[key])))
    if not obj[key]:
        del obj[key]
    target = rng.choice(objects(record))
    target_key = rng.choice(KEYS)
    current = target.get(target_key)
    if not isinstance(current, list):
        current = target[target_key] = []
    current.insert(rng.randint(0, len(current)), item)


def reshape(record: dict[str, Any], rng: random.Random, donor: dict[str, Any]) -> None:
    """Turn a one-item array into a single value, or a single object into an array."""
    slots = [
        (obj, key)
        for obj in objects(record)
        for key, value in obj.items()
        if key not in OPAQUE_KEYS
        and (is_structured(value) or (isinstance(value, list) and len(value) == 1))
    ]
    if slots:
        obj, key = rng.choice(slots)
        obj[key] = obj[key][0] if isinstance(obj[key], list) else [obj[key]]


def set_single(record: dict[str, Any], rng: random.Random, donor: dict[str, Any]) -> None:
    """Set a property to a single value of any shape, replacing whatever it held."""
    obj = rng.choice(objects(record))
    keys = [k for k in obj if k not in ("id", "type")] + KEYS
    key = rng.choice(keys)
    obj[key] = rng.choice(
        [
            "text",
            "",
            7,
            7.0,
            False,
            None,
            [],
            {"@value": 1},
            _new_ref(rng),
            _new_node(record, rng),
            ["x", "x"],
        ]
    )


def retype(record: dict[str, Any], rng: random.Random, donor: dict[str, Any]) -> None:
    """Change the ``type`` of an object (a replacement of the node, for the diff)."""
    obj = rng.choice(objects(record))
    obj["type"] = rng.choice(["Name", "Identifier", "Type", "Person", ["Name", "Type"]])


def graft(record: dict[str, Any], rng: random.Random, donor: dict[str, Any]) -> None:
    """Copy a property of another record into an object of this one."""
    keys = [k for k in donor if k not in ("id", "type")]
    if keys:
        key = rng.choice(keys)
        rng.choice(objects(record))[key] = copy.deepcopy(donor[key])


MUTATIONS: list[Mutation] = [
    edit_string,
    edit_string,
    delete_key,
    remove_item,
    reorder,
    reorder,
    reorder,
    add_item,
    add_item,
    repeat_item,
    move_item,
    reshape,
    set_single,
    retype,
    graft,
]


def mutate(
    record: dict[str, Any], rng: random.Random, donor: dict[str, Any], count: int
) -> tuple[dict[str, Any], list[str]]:
    """Return an edited deep copy of ``record`` and the names of the edits made."""
    edited = copy.deepcopy(record)
    names: list[str] = []
    for _ in range(count):
        mutation = rng.choice(MUTATIONS)
        mutation(edited, rng, donor)
        names.append(mutation.__name__)
    return assign_node_iris(edited, record["id"]), names
