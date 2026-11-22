"""Command-line entry point: ``lae <command>``.

Sub-command names match the process names in conventions §1, the entries in ``deploy/Procfile``,
and the ``deploy/systemd/lae-<name>.service`` units. Every command reads ``Settings``
(``LAE_*``). ``migrate`` runs ``alembic upgrade head`` against ``LAE_DATABASE_URL``; ``api``
configures JSON logging and runs uvicorn on ``lae.app.create_app``.

``crdt-ws``, ``sync-worker``, and ``scheduler`` run ``main(settings)`` from the module named in
``PROCESS_MODULES``; ``main`` may be a coroutine function and returns an exit code (``None``
means 0). Those modules are imported lazily: until the step that builds one has landed, the
command reports which step that is and exits with ``EXIT_NOT_IMPLEMENTED``, so ``make run`` and
systemd never mistake a placeholder for a running process.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import sys
from collections.abc import Callable, Sequence
from types import ModuleType

import uvicorn

from lae.app import create_app
from lae.db import migrate
from lae.logging import configure_logging
from lae.settings import Settings, get_settings

COMMANDS: dict[str, str] = {
    "api": "Run the HTTP API server.",
    "crdt-ws": "Run the CRDT WebSocket server.",
    "sync-worker": "Run the outbox-to-QLever sync worker.",
    "scheduler": "Run the periodic job scheduler.",
    "migrate": "Apply database migrations (alembic upgrade head).",
    "bootstrap": "Bulk-load records into Postgres and QLever.",
}

PROCESS_MODULES: dict[str, tuple[str, str]] = {
    "crdt-ws": ("lae.crdt.server", "1.8"),
    "sync-worker": ("lae.sync.worker", "1.11"),
    "scheduler": ("lae.scheduler.main", "1.15"),
}
"""Processes whose module exposes ``main(settings)``: command → (module, guide step building it)."""

NOT_IMPLEMENTED: dict[str, str] = {
    "bootstrap": "1.12",
}
"""One-shot commands not built yet: command → guide step that builds it."""

EXIT_NOT_IMPLEMENTED = 3
"""Exit code of a command whose implementing step has not landed."""


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with one sub-command per process."""
    parser = argparse.ArgumentParser(prog="lae", description="Collaborative Linked Art Editor")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    for name, help_text in COMMANDS.items():
        subparsers.add_parser(name, help=help_text, description=help_text)
    return parser


def not_implemented(command: str, step: str) -> int:
    """Tell the operator that ``command`` arrives with guide ``step``; return the exit code."""
    print(
        f"lae {command}: not implemented yet (Phase 1, Step {step}; "
        "see docs/phases/phase-1-core-loop.md)",
        file=sys.stderr,
    )
    return EXIT_NOT_IMPLEMENTED


def run_migrate() -> int:
    """``lae migrate``: apply every migration up to head."""
    migrate.upgrade("head")
    return 0


def run_api() -> int:
    """``lae api``: serve ``create_app(settings)`` with uvicorn until interrupted."""
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # keep the JSON logging configured above
        log_level=settings.log_level,
        access_log=False,  # RequestContextMiddleware logs each request with its request_id
        server_header=False,
    )
    return 0


def _import_process_module(module_name: str) -> ModuleType | None:
    """Import ``module_name``; return ``None`` only if that module (or a parent) is missing."""
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if module_name == missing or module_name.startswith(missing + "."):
            return None
        raise


def run_process(command: str) -> int:
    """Run ``main(settings)`` of the module behind a long-running process command."""
    module_name, step = PROCESS_MODULES[command]
    module = _import_process_module(module_name)
    if module is None:
        return not_implemented(command, step)
    settings: Settings = get_settings()
    configure_logging(settings.log_level)
    result = module.main(settings)
    if inspect.iscoroutine(result):
        result = asyncio.run(result)
    return 0 if result is None else int(result)


HANDLERS: dict[str, Callable[[], int]] = {
    "api": run_api,
    "migrate": run_migrate,
}
"""Sub-commands implemented in this module; the others are in ``PROCESS_MODULES``."""


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and run the selected sub-command; return the exit code."""
    args = build_parser().parse_args(argv)
    command: str = args.command
    handler = HANDLERS.get(command)
    if handler is not None:
        return handler()
    if command in PROCESS_MODULES:
        return run_process(command)
    return not_implemented(command, NOT_IMPLEMENTED[command])


if __name__ == "__main__":
    raise SystemExit(main())
