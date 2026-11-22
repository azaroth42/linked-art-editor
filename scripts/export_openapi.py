"""Write the OpenAPI document of the ``api`` application as JSON, without starting a server.

Run from the repository root with the backend environment:

    uv run --project backend python scripts/export_openapi.py [--output FILE]

The document is ``lae.app.openapi_document()``, identical to ``GET /openapi.json`` of a running
``lae api``. In ``frontend/``, ``corepack pnpm api:generate`` pipes it into ``openapi-typescript``
to regenerate ``src/api/schema.d.ts``, and ``corepack pnpm api:check`` (run in CI) fails when the
committed file differs from what the current backend generates.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

try:
    from lae.app import openapi_document
except ModuleNotFoundError as exc:  # pragma: no cover - wrong interpreter
    raise SystemExit(
        f"{exc}. Run with the backend environment: "
        "uv run --project backend python scripts/export_openapi.py"
    ) from exc


def main(argv: list[str] | None = None) -> int:
    """Write the OpenAPI document to ``--output`` or stdout and return the exit code."""
    parser = argparse.ArgumentParser(description="Write the API's OpenAPI document as JSON.")
    parser.add_argument("-o", "--output", type=Path, help="output file (default: stdout)")
    args = parser.parse_args(argv)
    # Anything the application prints while it is built goes to stderr, so stdout is pure JSON.
    with contextlib.redirect_stdout(sys.stderr):
        document = openapi_document()
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
