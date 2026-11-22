"""Command-line entry point: ``lae <command>``.

Sub-command names match the process names in conventions §1, the entries in ``deploy/Procfile``,
and the ``deploy/systemd/lae-<name>.service`` units. Every command is a placeholder that prints
its name and exits 0 until a later step wires it (``migrate`` in Step 0.5, the rest in Phase 1).
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

COMMANDS: dict[str, str] = {
    "api": "Run the HTTP API server.",
    "crdt-ws": "Run the CRDT WebSocket server.",
    "sync-worker": "Run the outbox-to-QLever sync worker.",
    "scheduler": "Run the periodic job scheduler.",
    "migrate": "Apply database migrations (alembic upgrade head).",
    "bootstrap": "Bulk-load records into Postgres and QLever.",
}


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with one sub-command per process."""
    parser = argparse.ArgumentParser(prog="lae", description="Collaborative Linked Art Editor")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    for name, help_text in COMMANDS.items():
        subparsers.add_parser(name, help=help_text, description=help_text)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and run the selected sub-command; return the exit code."""
    args = build_parser().parse_args(argv)
    command: str = args.command
    print(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
