"""Property test: random Linked-Art-shaped documents round-trip exactly.

``S3_MAX_EXAMPLES`` raises the example count (default 300) for the extended run in the report.
"""

from __future__ import annotations

import os
from typing import Any

from hypothesis import HealthCheck, event, given, settings
from pycrdt import Doc

from s3_projection.projection import (
    canonical_json,
    hydrate,
    is_node,
    is_reference,
    project,
    strip_internal_iris,
)
from s3_projection.strategies import RECORD_URI, records

MAX_EXAMPLES = int(os.environ.get("S3_MAX_EXAMPLES", "300"))


def _features(value: Any, seen: set[str], *, is_root: bool = True) -> None:
    """Record which rule-relevant shapes a document contains (for the statistics)."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "@context":
                seen.add(f"@context is {type(child).__name__}")
                continue
            if isinstance(child, list):
                kinds = {
                    "ref" if is_reference(i) else "node" if is_node(i) else "opaque" for i in child
                }
                seen.add("array: " + ("+".join(sorted(kinds)) or "empty"))
                keys = [canonical_json(i) for i in child]
                if len(set(keys)) < len(keys):
                    seen.add("array with an exact duplicate item")
                ids = [i["id"] for i in child if is_reference(i) or (is_node(i) and "id" in i)]
                if len(set(ids)) < len(ids):
                    seen.add("array with a repeated id")
            if is_node(child) and "id" in child:
                seen.add("embedded node with its own id")
            if isinstance(child, float) and child.is_integer():
                seen.add("integral float")
            if isinstance(child, int) and not isinstance(child, bool) and abs(child) > 2**53:
                seen.add("integer beyond 2^53")
            _features(child, seen, is_root=False)
    elif isinstance(value, list):
        for child in value:
            if is_node(child) and "id" in child:
                seen.add("embedded node with its own id")
            _features(child, seen, is_root=False)


@settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(records())
def test_random_documents_roundtrip(record: dict[str, Any]) -> None:
    seen: set[str] = set()
    _features(record, seen)
    for feature in sorted(seen):
        event(feature)

    original = canonical_json(record)
    doc = hydrate(record)
    projected = project(doc)
    assert canonical_json(strip_internal_iris(projected)) == original
    assert canonical_json(record) == original

    # Across the wire to a second replica.
    replica: Doc = Doc()
    replica.apply_update(doc.get_update())
    assert canonical_json(project(replica)) == canonical_json(projected)

    # The projection with its internal IRIs is a fixed point of hydrate/project.
    assert canonical_json(project(hydrate(projected, RECORD_URI))) == canonical_json(projected)
