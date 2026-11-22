"""Command-line entry point: ``lae <command>``.

Sub-command names match the process names in conventions §1, the entries in ``deploy/Procfile``,
and the ``deploy/systemd/lae-<name>.service`` units. ``migrate`` runs ``alembic upgrade head``
against ``LAE_DATABASE_URL``; the other commands are placeholders that print their name and exit 0
until Phase 1 wires them (each gets an entry in ``HANDLERS``).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence

from lae.db import migrate

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


def run_migrate() -> int:
    """``lae migrate``: apply every migration up to head."""
    migrate.upgrade("head")
    return 0


HANDLERS: dict[str, Callable[[], int]] = {
    "migrate": run_migrate,
}
"""Sub-commands that are implemented; the others are placeholders."""


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and run the selected sub-command; return the exit code."""
    args = build_parser().parse_args(argv)
    command: str = args.command
    handler = HANDLERS.get(command)
    if handler is not None:
        return handler()
    print(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
