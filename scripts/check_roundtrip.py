"""Check the corpus round-trip invariant on one record read from stdin.

The invariant of ``backend/tests/test_roundtrip.py``: ``hydrate`` → ``project`` →
``strip_internal_iris`` gives back the input exactly (as canonical JSON), and a second
``hydrate`` of the projection mints nothing new. ``frontend/e2e/collab.spec.ts`` (Step 1.22) runs
it on the JSON-LD a collaborative session published:

    uv run --project backend --locked python scripts/check_roundtrip.py < record.json

Prints ``{"ok": true}``, or ``{"ok": false, "expected": ..., "actual": ...}`` and exits 1.
"""

from __future__ import annotations

import json
import sys

try:
    from lae.projection import canonical_json, hydrate, project, strip_internal_iris
except ModuleNotFoundError as exc:  # pragma: no cover - wrong interpreter
    raise SystemExit(
        f"{exc}. Run with the backend environment: "
        "uv run --project backend python scripts/check_roundtrip.py"
    ) from exc


def main() -> int:
    record = json.load(sys.stdin)
    expected = canonical_json(record)
    projected = project(hydrate(record))
    actual = canonical_json(strip_internal_iris(projected))
    stable = canonical_json(project(hydrate(projected))) == canonical_json(projected)
    ok = actual == expected and stable
    result: dict[str, object] = {"ok": ok}
    if not ok:
        result.update(expected=expected.decode(), actual=actual.decode(), fixed_point=stable)
    print(json.dumps(result))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
