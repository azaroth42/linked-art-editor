"""Canonical JSON: the byte form in which two JSON-LD documents are compared (R16)."""

from __future__ import annotations

from typing import Any

import orjson


def canonical_json(d: Any) -> bytes:
    """Serialize ``d`` with sorted object keys; array order is kept exactly."""
    return orjson.dumps(d, option=orjson.OPT_SORT_KEYS)
