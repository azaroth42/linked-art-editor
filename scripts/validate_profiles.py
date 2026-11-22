"""Validate profiles/ui and profiles/vocab YAML against their JSON Schemas.

Run from the repository root with the backend environment:

    uv run --project backend python scripts/validate_profiles.py [--profiles-dir DIR]

Exits 0 when every file is valid and 1 otherwise, printing one line per problem. The checks
live in ``lae.profiles.validation`` so the API can run the same ones at startup; the list of
checks is in profiles/README.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from lae.profiles.validation import validate_profiles_dir
except ModuleNotFoundError as exc:  # pragma: no cover - wrong interpreter
    raise SystemExit(
        f"{exc}. Run with the backend environment: "
        "uv run --project backend python scripts/validate_profiles.py"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    """Validate the profiles directory and return the process exit code."""
    parser = argparse.ArgumentParser(description="Validate UI Profile and Value Set YAML.")
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=REPO_ROOT / "profiles",
        help="profiles directory to validate (default: the repository's profiles/)",
    )
    args = parser.parse_args(argv)
    report = validate_profiles_dir(args.profiles_dir)
    for note in report.notes:
        print(f"note: {note}")
    if not report.ok:
        for error in report.errors:
            print(f"error: {error}", file=sys.stderr)
        print(
            f"FAILED: {len(report.errors)} error(s) in {report.files_checked} file(s)",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {report.files_checked} profile file(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
