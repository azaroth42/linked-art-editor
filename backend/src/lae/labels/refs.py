"""Finding and rewriting the references in a Linked Art record, for labels.

Here a reference is any object below the root, outside ``@context``, whose ``id`` is a string.
That covers plain stubs (``{"id", "type", "_label"}``) and also references that carry more
(a ``Type`` with its metatype ``classified_as``, a ``Language`` with a ``notation``). Their
``_label`` is equally a copy of the target's label, so it is equally stripped when the target is
in ``entity_labels`` and transcluded on read (ADR-0019). Internal node IRIs never have an
``entity_labels`` row, so embedded nodes are never changed. The root is not a reference.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator, Mapping
from typing import Any

CONTEXT_KEY = "@context"

ReferenceRewrite = Callable[[dict[str, Any]], dict[str, Any]]


def is_labelled_reference(value: Any) -> bool:
    """True for an object whose ``id`` is a string (when it is below a record's root)."""
    return isinstance(value, dict) and isinstance(value.get("id"), str)


def _iter_below(value: Any) -> Iterator[dict[str, Any]]:
    """Yield every reference in ``value`` itself or nested in it, in document order."""
    if isinstance(value, dict):
        if is_labelled_reference(value):
            yield value
        for key, child in value.items():
            if key != CONTEXT_KEY:
                yield from _iter_below(child)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_below(item)


def iter_references(jsonld: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield every reference below the root of ``jsonld``, in document order (outer first)."""
    for key, value in jsonld.items():
        if key != CONTEXT_KEY:
            yield from _iter_below(value)


def reference_ids(jsonld: Mapping[str, Any]) -> set[str]:
    """Return the target ``id`` of every reference below the root of ``jsonld``."""
    return {reference["id"] for reference in iter_references(jsonld)}


def _rewrite_below(value: Any, rewrite: ReferenceRewrite) -> Any:
    """Return a copy of ``value`` in which every reference was passed through ``rewrite``."""
    if isinstance(value, dict):
        rebuilt = {
            key: copy.deepcopy(item) if key == CONTEXT_KEY else _rewrite_below(item, rewrite)
            for key, item in value.items()
        }
        return rewrite(rebuilt) if is_labelled_reference(rebuilt) else rebuilt
    if isinstance(value, list):
        return [_rewrite_below(item, rewrite) for item in value]
    return copy.deepcopy(value)


def rewrite_references(jsonld: Mapping[str, Any], rewrite: ReferenceRewrite) -> dict[str, Any]:
    """Return a deep copy of ``jsonld`` with each reference below the root passed through
    ``rewrite`` (which receives a fresh copy it may change); ``jsonld`` is not modified."""
    return {
        key: copy.deepcopy(value) if key == CONTEXT_KEY else _rewrite_below(value, rewrite)
        for key, value in jsonld.items()
    }
