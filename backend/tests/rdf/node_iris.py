"""TEST-ONLY stand-in for the projection's ``assign_node_iris``. Not for use outside tests.

Step 1.4 (``lae/projection``) is being implemented in parallel with Step 1.3; its
``assign_node_iris`` replaces this helper, which should then be deleted and the RDF tests pointed
at the real function. It follows Spike S3 rules R3 and R4 (``docs/spikes/S3-projection.md``):
every node other than the root that has a ``type`` and no ``id`` gets
``<record_uri>/node/<uuidv7>``; references, nodes that already have an ``id``, opaque values and
``@context`` are left alone.

Unlike the real function it mints ids from a counter, so that two processes assign the same IRIs
and expansion can be compared across them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from itertools import count
from typing import Any
from uuid import UUID

from lae.corpus import iter_corpus
from lae.ids import NODE_PATH

REFERENCE_KEYS = frozenset({"id", "type", "_label"})


def counter_mint(record_uri: str) -> Callable[[], str]:
    """Return a mint giving ``<record_uri>/node/<n-th well-formed UUIDv7>`` for n = 1, 2, ..."""
    numbers = count(1)

    def mint() -> str:
        node_id = UUID(f"00000000-0000-7000-8000-{next(numbers):012x}")
        return f"{record_uri}{NODE_PATH}{node_id}"

    return mint


def _is_reference(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and value.keys() <= REFERENCE_KEYS
    )


def _is_node(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and "type" in value
        and ("id" not in value or isinstance(value["id"], str))
        and not _is_reference(value)
    )


def _assign(obj: dict[str, Any], mint: Callable[[], str], *, is_root: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in obj.items():
        if key == "@context":
            out[key] = value
        elif _is_node(value):
            out[key] = _assign(value, mint, is_root=False)
        elif isinstance(value, list):
            out[key] = [_assign(i, mint, is_root=False) if _is_node(i) else i for i in value]
        else:
            out[key] = value
    if not is_root and "id" not in out:
        out["id"] = mint()
    return out


def assign_node_iris(jsonld: dict[str, Any], record_uri: str) -> dict[str, Any]:
    """Return a copy of ``jsonld`` with an internal node IRI on every embedded node lacking one."""
    return _assign(jsonld, counter_mint(record_uri), is_root=True)


def corpus_with_node_iris() -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(file name, record with node IRIs)`` for every corpus record, in a stable order."""
    for path, record in iter_corpus():
        yield path.name, assign_node_iris(record, record["id"])
