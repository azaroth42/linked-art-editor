"""Spike S3 cross-language check: does real Yjs read, project, edit and re-encode pycrdt state?

Manual (it needs ``npm install yjs``, so it is not in the pytest run). Results are in
``docs/spikes/S3-projection.md`` section 4.4. From ``backend/``, with ``W`` a scratch directory::

    mkdir -p $W/js && (cd $W/js && npm init -y && npm install yjs@13)
    cp ../scripts/spikes/s3_projection/js_interop/interop.mjs $W/js/
    uv run python ../scripts/spikes/s3_projection/js_interop/check_interop.py \
        export $W/fixtures.json
    node $W/js/interop.mjs $W/fixtures.json $W/out.json
    uv run python ../scripts/spikes/s3_projection/js_interop/check_interop.py \
        verify $W/fixtures.json $W/out.json
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

from pycrdt import Doc, Map

from lae.corpus import iter_corpus

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from s3_projection.projection import (  # noqa: E402
    ROOT_MAP,
    canonical_json,
    entry_key,
    hydrate,
    insert_entry,
    project,
    strip_internal_iris,
)

EXTRA: dict[str, Any] = {
    "id": "https://example.org/id/extra",
    "type": "HumanMadeObject",
    "@context": ["https://linked.art/ns/v1/linked-art.json", {"x": "https://example.org/x"}],
    "identified_by": [{"type": "Name", "content": "a\U0001f600"}, {"type": "Name", "content": ""}],
    "numbers": [0, 1, -1, 33.0, -0.0, 33.5, 2**53, 2**53 + 1, 2**64 - 1, 1e21, 5e-324, 0.1],
    "value": 2550,
    "value_f": 2550.0,
    "flags": [True, False, None, "true", "a", "a"],
    "plain": {"b": 1, "a": 1.0, "nested": [1, 2.0, {"type": "Name"}]},
    "empty": [],
    "refs": [{"id": "x"}, {"id": "～"}, {"id": "\U0001f600"}, {"id": "x"}],
    # Keys that share the "[" prefix of entry keys, and one that JS objects treat specially.
    "[": "single",
    '["a","b"]': [{"id": '["a","b"]'}, "["],
    "__proto__": [{"id": "__proto__"}],
    "single": {"type": "Type", "__proto__": "x", "constructor": ["y"]},
}

# An array property that no corpus record and no fixture has.
FIRST_PROP = "x_s3_first"

# (property, item key) pairs whose entry key must be the same text in Python and JavaScript.
KEY_PARTS = [
    ["classified_as", "http://vocab.getty.edu/aat/300404670"],
    ["identified_by", "https://example.org/id/a/node/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f"],
    ["a", "x #2"],
    ['q"uo\\te', "=s:line\nbreak\ttab\r\b\f"],
    ["\u0000\u0001\u001f\u007f\u0080  ", "/<>&'﻿￾￿"],
    ["é～\U0001f600", "=j:" + '{"a":[1,2.5,"x"]}'],
    ["[", "]"],
    ["", ""],
]


def documents() -> list[tuple[str, dict[str, Any]]]:
    docs = [(f"{p.parent.name}/{p.name}", record) for p, record in iter_corpus()]
    return [*docs, ("extra", EXTRA)]


def _as_doubles(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, dict):
        return {k: _as_doubles(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_as_doubles(v) for v in value]
    return value


def _python_first_insert(doc: Doc[Any], record_uri: str) -> bytes:
    """The update of a second Python client that makes the first insert into ``FIRST_PROP``."""
    other: Doc[Any] = Doc()
    other.apply_update(doc.get_update())
    before = other.get_state()
    insert_entry(
        other,
        other.get(ROOT_MAP, type=Map),
        FIRST_PROP,
        {"type": "Name", "content": "first from Python"},
        record_uri=record_uri,
    )
    return other.get_update(before)


def export(fixtures: Path) -> None:
    out = []
    for name, record in documents():
        doc = hydrate(record)
        out.append(
            {
                "name": name,
                "record_uri": record["id"],
                "update": base64.b64encode(doc.get_update()).decode(),
                "projected": project(doc),
                "python_first": base64.b64encode(_python_first_insert(doc, record["id"])).decode(),
            }
        )
    fixtures.write_text(
        json.dumps({"documents": out, "first_prop": FIRST_PROP, "key_parts": KEY_PARTS})
    )
    print(f"exported {len(out)} documents")


def verify(fixtures: Path, results: Path) -> int:
    expected = {f["name"]: f for f in json.loads(fixtures.read_text())["documents"]}
    sources = dict(documents())
    out = json.loads(results.read_text())
    js_project = reencode = inserts = firsts = 0
    failures: list[str] = []
    for result in out["results"]:
        name = result["name"]
        fixture = expected[name]
        source = sources[name]
        # 1. The JS project() mirror sees the same document. Numbers are compared as doubles
        #    (JS has one number type, so 33.0 is 33 and 2**53 + 1 is 2**53 there); everything
        #    else exactly.
        if _as_doubles(result["projected"]) == _as_doubles(fixture["projected"]):
            js_project += 1
        else:
            failures.append(f"{name}: JS projection differs")
        # 2. State re-encoded by Yjs still projects to the exact source in Python.
        doc: Doc[Any] = Doc()
        doc.apply_update(base64.b64decode(result["reencoded"]))
        if canonical_json(strip_internal_iris(project(doc))) == canonical_json(source):
            reencode += 1
        else:
            failures.append(f"{name}: Yjs re-encoding changed the canonical projection")
        # 3. A Name inserted by JS at position 1 lands at position 1 in Python.
        base: Doc[Any] = Doc()
        base.apply_update(base64.b64decode(fixture["update"]))
        inserted = {"type": "Name", "content": "from JS \U0001f600", "value": 34}
        if result["insert"] is not None:
            base.apply_update(base64.b64decode(result["insert"]["update"]))
            names = project(base)["identified_by"]
            want = strip_internal_iris(fixture["projected"])["identified_by"]
            got = strip_internal_iris(project(base))["identified_by"]
            ok = got == [want[0], inserted, *want[1:]] and type(names[1]["value"]) is int
            if ok:
                inserts += 1
            else:
                failures.append(f"{name}: JS insert at position 1 misplaced")
        # 4. JS and Python each make the first insert into the same absent property: both
        #    entries survive in both orders of delivery, and JS and Python project the same.
        updates = [base64.b64decode(result["first"]["update"])]
        updates.append(base64.b64decode(fixture["python_first"]))
        merged = []
        for order in (updates, updates[::-1]):
            replica: Doc[Any] = Doc()
            replica.apply_update(base.get_update())
            for update in order:
                replica.apply_update(update)
            merged.append(project(replica))
        got_first = sorted(n["content"] for n in merged[0].get(FIRST_PROP, []))
        if (
            got_first == ["first from JS", "first from Python"]
            and canonical_json(merged[0]) == canonical_json(merged[1])
            and _as_doubles(result["merged"]) == _as_doubles(merged[0])
        ):
            firsts += 1
        else:
            failures.append(f"{name}: concurrent first inserts from JS and Python: {got_first}")
    total = len(out["results"])
    with_names = sum(1 for r in out["results"] if r["insert"] is not None)
    tie_ok = out["tie"] == sorted(out["tie"]) == ["a", "b", "～", "\U0001f600"]
    keys_ok = out["keys"] == [entry_key(prop, item) for prop, item in KEY_PARTS]
    print(f"JS projection equal to Python projection: {js_project}/{total}")
    print(f"exact round-trip after Yjs re-encoding:  {reencode}/{total}")
    print(f"JS midpoint insert at position 1:         {inserts}/{with_names}")
    print(f"concurrent first inserts, JS and Python:  {firsts}/{total}")
    print(f"code-point tie-break order matches:       {tie_ok}")
    print(f"entry keys equal to JSON.stringify:       {keys_ok} ({len(KEY_PARTS)} pairs)")
    for failure in failures[:20]:
        print("FAIL", failure)
    return 0 if not failures and tie_ok and keys_ok and total == len(sources) else 1


if __name__ == "__main__":
    if sys.argv[1] == "export":
        export(Path(sys.argv[2]))
    else:
        sys.exit(verify(Path(sys.argv[2]), Path(sys.argv[3])))
