"""The oracle: every golden corpus file round-trips through the Y.Doc exactly."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pycrdt import Doc

from lae.corpus import corpus_files, load_record
from lae.ids import is_internal_node_iri
from lae.projection.canonical_json import canonical_json
from lae.projection.hydrate import hydrate
from lae.projection.project import project, strip_internal_iris
from lae.projection.rules import is_node

FILES = corpus_files()


def _embedded_nodes(value: Any, *, is_root: bool = True) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if not is_root and is_node(value):
            found.append(value)
        for key, child in value.items():
            if key != "@context":
                found.extend(_embedded_nodes(child, is_root=False))
    elif isinstance(value, list):
        for child in value:
            found.extend(_embedded_nodes(child, is_root=False))
    return found


def test_corpus_is_complete() -> None:
    assert len(FILES) == 276


@pytest.mark.parametrize("path", FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_roundtrip(path: Path) -> None:
    record = load_record(path)
    original = canonical_json(record)
    doc = hydrate(record)

    # The guide's oracle.
    projected = project(doc)
    assert canonical_json(strip_internal_iris(projected)) == original

    # hydrate() must not mutate its input.
    assert canonical_json(record) == original

    # Internal IRIs are kept by project(): every embedded node has an id, and the ids that
    # were not in the source are internal node IRIs of this record.
    source_ids = {n["id"] for n in _embedded_nodes(record) if "id" in n}
    for node in _embedded_nodes(projected):
        assert "id" in node
        assert node["id"] in source_ids or is_internal_node_iri(node["id"], record["id"])
    # No source id looks like an internal IRI (it would be stripped on output).
    assert not any(is_internal_node_iri(i, record["id"]) for i in source_ids)

    # The same holds after the state crosses the wire into another replica.
    replica: Doc = Doc()
    replica.apply_update(doc.get_update())
    assert canonical_json(project(replica)) == canonical_json(projected)

    # Re-hydrating a projection mints nothing new: the projection (with IRIs) is a fixed point.
    assert canonical_json(project(hydrate(projected))) == canonical_json(projected)
