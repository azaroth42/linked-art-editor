"""Runs the shared operation fixtures in ``fixtures/*.json`` (also the format's definition).

The TypeScript mirror (``frontend/src/crdt/paths.ts``, Step 1.17) loads the same files unchanged
and must reproduce every ``returns``, ``error``, ``expected`` and ``expected_state``.

A fixture is one JSON object:

``description``, ``rules``
    What the case shows and which rules of ``docs/spikes/S3-projection.md`` it exercises.
``record_uri``
    The record URI; also the root ``id`` of ``initial``.
``initial``
    The JSON-LD the server hydrated. Every embedded node already has an ``id``, so hydration
    mints nothing and the fixture is deterministic.
``initial_update``
    Base64 of the Y update (v1 encoding) of ``hydrate(initial)``. Every client starts by
    applying it; a client never hydrates for itself (R2).
``initial_state``
    The state dump (below) of the hydrated document.
``clients``
    Object from client name to Y client id. Set the id before applying ``initial_update``
    (``Doc(client_id=n)`` / ``doc.clientID = n``): where two clients write the same register
    concurrently the winner depends on it.
``steps``
    Run in order. A step is either ``{"sync": [names]}``, which exchanges updates among the named
    clients until they all hold the same state, or an operation::

        {"client": name, "op": name, "args": {...}, "returns": value}
        {"client": name, "op": name, "args": {...}, "error": "LAE-PROJ-00n"}

    ``args`` are the operation's arguments by name. Three are special. ``base`` is a list of
    hops from the root map to the object map the path starts from: ``{"path": p, "key": k}`` is
    the entry with entry key ``k`` of the array ``p`` addresses, and ``{"path": p}`` is the one
    node ``p`` addresses. ``mint`` is the list of internal node IRIs the operation mints, in
    order (first the nodes of the item being added, each nested node before the node that
    contains it and siblings in key order, then the missing intermediate nodes of the path from
    the outermost in); an operation that succeeds uses all of them. ``type`` and ``label`` of
    ``add_reference`` are optional.

    ``returns`` is the operation's result, present for every operation that does not fail
    (``null`` for operations without a result). ``error`` is the code of the ``ProjectionError``
    the operation raises; a failed operation leaves the document unchanged. Besides the
    operations of ``lae.projection.ops`` there are three read-only ones: ``project`` (returns the
    client's projection), ``get_path`` (returns the JSON of each addressed value) and
    ``list_entries`` (returns the entry keys).

    Numbers in ``args`` are never floats with an integral value: JSON cannot tell ``33.0`` from
    ``33``, and Python stores the two differently (R9).
``expected``
    ``project(doc)`` of every client after the last step (internal node IRIs kept).
``expected_state``
    The state dump of every client after the last step.

State dump: a shared map is ``{"map": {key: dump, ...}}`` with the map keys exactly as stored
(entry keys included), a shared text is ``{"text": string}``, and a plain value is
``{"plain": value}`` with the stored value (a string, number, boolean, ``null`` or a ``_json``
wrapper). Numbers compare as IEEE doubles.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pycrdt import Doc, Map, Text

from lae.projection import ops
from lae.projection.hydrate import hydrate
from lae.projection.project import project, record_map
from lae.projection.rules import ProjectionError

FIXTURE_DIR = Path(__file__).parent / "fixtures"

MUTATIONS: dict[str, Callable[..., Any]] = {
    "set_scalar": ops.set_scalar,
    "set_object": ops.set_object,
    "remove_value": ops.remove_value,
    "splice_text": ops.splice_text,
    "add_node": ops.add_node,
    "add_reference": ops.add_reference,
    "remove_entry": ops.remove_entry,
    "move_entry": ops.move_entry,
}


def fixture_files() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


def dump_state(value: Any) -> Any:
    """The state dump of a shared or plain value (see the module docstring)."""
    if isinstance(value, Map):
        return {"map": {key: dump_state(value[key]) for key in sorted(value.keys())}}
    if isinstance(value, Text):
        return {"text": str(value)}
    return {"plain": value}


def doc_state(doc: Doc[Any]) -> Any:
    return dump_state(record_map(doc))


def encode_update(doc: Doc[Any]) -> str:
    return base64.b64encode(doc.get_update()).decode("ascii")


def sync(docs: list[Doc[Any]]) -> None:
    """Exchange updates until every replica has every other replica's changes."""
    for _ in range(2):
        for source in docs:
            for target in docs:
                if source is not target:
                    target.apply_update(source.get_update(target.get_state()))


class _Mint:
    def __init__(self, iris: list[str]) -> None:
        self.iris = list(iris)

    def __call__(self) -> str:
        assert self.iris, "the operation minted more node IRIs than the fixture lists"
        return self.iris.pop(0)


def _resolve_base(doc: Doc[Any], hops: list[dict[str, str]]) -> Map[Any]:
    base = record_map(doc)
    for hop in hops:
        if "key" in hop:
            base = ops.get_entry(doc, hop["path"], hop["key"], base=base)
        else:
            (found,) = ops.get_path(doc, hop["path"], base=base)
            assert isinstance(found, Map)
            base = found
    return base


def _call(doc: Doc[Any], op: str, args: dict[str, Any]) -> Any:
    kwargs = dict(args)
    base = _resolve_base(doc, kwargs.pop("base")) if "base" in kwargs else None
    if op == "project":
        return project(doc)
    if op == "get_path":
        return [ops.value_to_json(v) for v in ops.get_path(doc, kwargs["path"], base=base)]
    if op == "list_entries":
        return [key for key, _ in ops.list_entries(doc, kwargs["path"], base=base)]
    function = MUTATIONS[op]
    mint = _Mint(kwargs.pop("mint")) if "mint" in kwargs else None
    if op in ("set_scalar", "set_object", "add_node", "add_reference"):
        kwargs["mint"] = mint or _Mint([])
    result = function(doc, base=base, **kwargs)
    assert mint is None or not mint.iris, "the operation minted fewer node IRIs than listed"
    return result


def run_fixture(fixture: dict[str, Any], *, record: bool = False) -> dict[str, Any]:
    """Run one fixture and compare every expectation; with ``record`` fill them in instead.

    Returns the fixture (the same object, completed when recording).
    """
    hydrated = hydrate(fixture["initial"], fixture["record_uri"])
    if record:
        fixture["initial_update"] = encode_update(hydrated)
        fixture["initial_state"] = doc_state(hydrated)
    initial_update = base64.b64decode(fixture["initial_update"])
    docs: dict[str, Doc[Any]] = {}
    for name, client_id in fixture["clients"].items():
        doc: Doc[Any] = Doc(client_id=client_id)
        doc.apply_update(initial_update)
        assert doc_state(doc) == fixture["initial_state"] == doc_state(hydrated)
        assert project(doc) == fixture["initial"]
        docs[name] = doc

    for number, step in enumerate(fixture["steps"]):
        where = f"step {number}: {json.dumps(step, ensure_ascii=False)[:200]}"
        if "sync" in step:
            sync([docs[name] for name in step["sync"]])
            continue
        doc = docs[step["client"]]
        before = doc_state(doc)
        try:
            result = _call(doc, step["op"], step.get("args", {}))
        except ProjectionError as exc:
            assert doc_state(doc) == before, f"a failed operation changed the document; {where}"
            if record:
                step.pop("returns", None)
                step["error"] = exc.code
            assert step.get("error") == exc.code, f"{exc}; {where}"
            continue
        if record:
            step.pop("error", None)
            step["returns"] = result
        assert "error" not in step, f"expected {step.get('error')}; {where}"
        # Through JSON, so that tuples, ints and floats compare as the file stores them.
        assert json.loads(json.dumps(result)) == step["returns"], where

    states = {name: doc_state(doc) for name, doc in docs.items()}
    projections = {name: project(doc) for name, doc in docs.items()}
    first = next(iter(docs))
    if record:
        fixture["expected"] = projections[first]
        fixture["expected_state"] = states[first]
    for name in docs:
        assert projections[name] == fixture["expected"], f"client {name} projection"
        assert states[name] == fixture["expected_state"], f"client {name} state"
    return fixture
