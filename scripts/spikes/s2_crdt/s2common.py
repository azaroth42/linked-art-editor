"""Helpers shared by the Spike S2 server, load generator, probes and browser peer."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def decode_state_vector(state: bytes) -> dict[str, int]:
    """Decode a v1 state vector into ``{client_id: clock}``.

    Byte-comparing state vectors is not meaningful: yrs encodes clients in hash-map order and
    Yjs sorts them, so equal vectors can differ byte-wise. Compare the decoded mapping instead.
    """
    pos = 0

    def read_uint() -> int:
        nonlocal pos
        num, shift = 0, 0
        while True:
            byte = state[pos]
            pos += 1
            num |= (byte & 0x7F) << shift
            if byte < 0x80:
                return num
            shift += 7

    count = read_uint()
    result: dict[str, int] = {}
    for _ in range(count):
        client = read_uint()
        result[str(client)] = read_uint()
    return result


def content_digest(content: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of projected spike content (``fields`` + ``notes``)."""
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def pct(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile ``q`` (0-100) of ``values``; None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q / 100 * (len(ordered) - 1)))))
    return ordered[index]


def summarize(values: list[float]) -> dict[str, float | int | None]:
    """Count, p50, p95, p99 and max of a sample, rounded for reporting."""

    def r(x: float | None) -> float | None:
        return None if x is None else round(x, 2)

    return {
        "n": len(values),
        "p50": r(pct(values, 50)),
        "p95": r(pct(values, 95)),
        "p99": r(pct(values, 99)),
        "max": r(max(values) if values else None),
    }
