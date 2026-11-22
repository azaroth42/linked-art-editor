"""Shared helpers for the validation tests.

Every diagnostic is built by ``lae.validation.diagnostics.diagnostic``, which looks its English
message up with ``message_en``. The autouse fixture below wraps that lookup for the whole session
and records each code, so ``test_zz_error_messages.py`` (collected last in this directory) can
fail on any code the tests emitted that has no entry in ``errors.json``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from lae.projection.hydrate import assign_node_iris
from lae.validation import diagnostics

DATA_DIR = Path(__file__).parent / "data"
SHAPE_CASES: dict[str, Any] = json.loads((DATA_DIR / "shape_cases.json").read_text())

EMITTED_CODES: set[str] = set()
"""Every diagnostic code built while the tests of this directory ran (in this process)."""


@pytest.fixture(scope="session", autouse=True)
def record_emitted_codes() -> Iterator[None]:
    original = diagnostics.message_en

    def recording(code: str, params: Mapping[str, Any], fallback: str | None = None) -> str:
        EMITTED_CODES.add(code)
        return original(code, params, fallback)

    diagnostics.message_en = recording
    try:
        yield
    finally:
        diagnostics.message_en = original


def apply(base: dict[str, Any], operations: list[list[Any]]) -> dict[str, Any]:
    """Apply JSON Pointer 'set' operations to a copy of ``base``; a null value removes the key."""
    doc = copy.deepcopy(base)
    for pointer, value in operations:
        *parents, last = pointer.lstrip("/").split("/")
        node: Any = doc
        for part in parents:
            node = node[int(part)] if isinstance(node, list) else node[part]
        key = int(last) if isinstance(node, list) else last
        if value is None:
            del node[key]
        else:
            node[key] = value
    return doc


def base(name: str = "HumanMadeObject", *operations: list[Any]) -> dict[str, Any]:
    """A copy of one schema-valid base record of ``shape_cases.json``, with operations applied."""
    return apply(SHAPE_CASES["bases"][name], list(operations))


def with_node_iris(doc: dict[str, Any]) -> dict[str, Any]:
    """The document as the projection holds it: every embedded node has an internal node IRI."""
    return assign_node_iris(doc, doc["id"])


def found(items: Any) -> list[tuple[str, str, str]]:
    """``(code, severity, path)`` of each diagnostic, sorted."""
    return sorted((item.code, item.severity, item.path) for item in items)
