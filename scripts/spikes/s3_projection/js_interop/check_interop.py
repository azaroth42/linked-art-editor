"""Spike S3 cross-language check: does real Yjs read, project, edit and re-encode pycrdt state?

Manual (it needs ``npm install yjs``, so it is not in the pytest run). Results are in
``docs/spikes/S3-projection.md`` section 4.4. From ``backend/``::

    mkdir -p /tmp/lae-i8/js && (cd /tmp/lae-i8/js && npm init -y && npm install yjs@13)
    cp ../scripts/spikes/s3_projection/js_interop/interop.mjs /tmp/lae-i8/js/
    uv run python ../scripts/spikes/s3_projection/js_interop/check_interop.py \
        export /tmp/lae-i8/fixtures.json
    node /tmp/lae-i8/js/interop.mjs /tmp/lae-i8/fixtures.json /tmp/lae-i8/out.json
    uv run python ../scripts/spikes/s3_projection/js_interop/check_interop.py \
        verify /tmp/lae-i8/fixtures.json /tmp/lae-i8/out.json
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

from pycrdt import Doc

from lae.corpus import iter_corpus

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from s3_projection.projection import (  # noqa: E402
    canonical_json,
    hydrate,
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
}


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
            }
        )
    fixtures.write_text(json.dumps(out))
    print(f"exported {len(out)} documents")


def verify(fixtures: Path, results: Path) -> int:
    expected = {f["name"]: f for f in json.loads(fixtures.read_text())}
    sources = dict(documents())
    out = json.loads(results.read_text())
    js_project = reencode = inserts = 0
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
        doc: Doc = Doc()
        doc.apply_update(base64.b64decode(result["reencoded"]))
        if canonical_json(strip_internal_iris(project(doc))) == canonical_json(source):
            reencode += 1
        else:
            failures.append(f"{name}: Yjs re-encoding changed the canonical projection")
        # 3. A Name inserted by JS at position 1 lands at position 1 in Python.
        if result["insert"] is not None:
            base: Doc = Doc()
            base.apply_update(base64.b64decode(fixture["update"]))
            base.apply_update(base64.b64decode(result["insert"]["update"]))
            names = project(base)["identified_by"]
            want = strip_internal_iris(fixture["projected"])["identified_by"]
            got = strip_internal_iris(project(base))["identified_by"]
            inserted = {"type": "Name", "content": "from JS \U0001f600", "value": 34}
            ok = got == [want[0], inserted, *want[1:]] and type(names[1]["value"]) is int
            if ok:
                inserts += 1
            else:
                failures.append(f"{name}: JS insert at position 1 misplaced")
    total = len(out["results"])
    with_names = sum(1 for r in out["results"] if r["insert"] is not None)
    tie_ok = out["tie"] == sorted(out["tie"]) == ["a", "b", "～", "\U0001f600"]
    print(f"JS projection equal to Python projection: {js_project}/{total}")
    print(f"exact round-trip after Yjs re-encoding:  {reencode}/{total}")
    print(f"JS midpoint insert at position 1:         {inserts}/{with_names}")
    print(f"code-point tie-break order matches:       {tie_ok}")
    for failure in failures[:20]:
        print("FAIL", failure)
    return 0 if not failures and tie_ok and total == len(sources) else 1


if __name__ == "__main__":
    if sys.argv[1] == "export":
        export(Path(sys.argv[2]))
    else:
        sys.exit(verify(Path(sys.argv[2]), Path(sys.argv[3])))
