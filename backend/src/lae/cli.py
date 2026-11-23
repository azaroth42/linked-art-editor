"""Command-line entry point: ``lae <command>``.

Sub-command names match the process names in conventions §1, the entries in ``deploy/Procfile``,
and the ``deploy/systemd/lae-<name>.service`` units. Every command reads ``Settings``
(``LAE_*``). ``migrate`` runs ``alembic upgrade head`` against ``LAE_DATABASE_URL``;
``bootstrap <stage>`` runs one stage of the bulk bootstrap (``lae.bootstrap.cli``); ``vocab
<action>`` imports, exports and refreshes the Value Set registry (``lae.vocab.cli``); ``api``
configures JSON logging and runs uvicorn on ``lae.app.create_app``, and refuses to start (exit
``EXIT_CONFIG_ERROR``) when the authentication settings are unsafe, e.g. ``LAE_DEV_USER`` with
``LAE_ENV=production`` (``lae.auth.setup.AuthConfigError``).

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
from lae.auth.setup import AuthConfigError
from lae.bootstrap import cli as bootstrap_cli
from lae.db import migrate
from lae.logging import configure_logging
from lae.settings import Settings, get_settings
from lae.vocab import cli as vocab_cli

COMMANDS: dict[str, str] = {
    "api": "Run the HTTP API server.",
    "crdt-ws": "Run the CRDT WebSocket server.",
    "sync-worker": "Run the outbox-to-QLever sync worker.",
    "scheduler": "Run the periodic job scheduler.",
    "migrate": "Apply database migrations (alembic upgrade head).",
    "bootstrap": "Bulk-load records into Postgres and QLever.",
    "vocab": "Import, export and refresh the Value Set registry.",
}

PROCESS_MODULES: dict[str, tuple[str, str]] = {
    "crdt-ws": ("lae.crdt.server", "1.8"),
    "sync-worker": ("lae.sync.worker", "1.11"),
    "scheduler": ("lae.scheduler.main", "1.15"),
}
"""Processes whose module exposes ``main(settings)``: command → (module, guide step building it)."""

NOT_IMPLEMENTED: dict[str, str] = {}
"""One-shot commands not built yet: command → guide step that builds it."""

EXIT_NOT_IMPLEMENTED = 3
"""Exit code of a command whose implementing step has not landed."""

EXIT_CONFIG_ERROR = 2
"""Exit code of a process that refuses to start because of its settings."""


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with one sub-command per process."""
    parser = argparse.ArgumentParser(prog="lae", description="Laertes")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    for name, help_text in COMMANDS.items():
        subparser = subparsers.add_parser(name, help=help_text, description=help_text)
        if name == "bootstrap":
            bootstrap_cli.add_arguments(subparser)
        elif name == "vocab":
            vocab_cli.add_arguments(subparser)
    return parser


def not_implemented(command: str, step: str) -> int:
    """Tell the operator that ``command`` arrives with guide ``step``; return the exit code."""
    print(
        f"lae {command}: not implemented yet (Phase 1, Step {step}; "
        "see docs/phases/phase-1-core-loop.md)",
        file=sys.stderr,
    )
    return EXIT_NOT_IMPLEMENTED


def run_migrate(args: argparse.Namespace) -> int:
    """``lae migrate``: apply every migration up to head."""
    migrate.upgrade("head")
    return 0


def run_bootstrap(args: argparse.Namespace) -> int:
    """``lae bootstrap <stage>``: run one bulk bootstrap stage (Phase 1, Step 1.12)."""
    return bootstrap_cli.run(args)


def run_vocab(args: argparse.Namespace) -> int:
    """``lae vocab <action>``: manage the Value Set registry (Phase 2, Step 2.1)."""
    return vocab_cli.run(args)


def run_api(args: argparse.Namespace) -> int:
    """``lae api``: serve ``create_app(settings)`` with uvicorn until interrupted."""
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        app = create_app(settings)
    except AuthConfigError as exc:
        print(f"lae api: refusing to start: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    uvicorn.run(
        app,
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


HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "api": run_api,
    "migrate": run_migrate,
    "bootstrap": run_bootstrap,
    "vocab": run_vocab,
}
"""Sub-commands implemented in this module; the others are in ``PROCESS_MODULES``."""


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and run the selected sub-command; return the exit code."""
    args = build_parser().parse_args(argv)
    command: str = args.command
    handler = HANDLERS.get(command)
    if handler is not None:
        return handler(args)
    if command in PROCESS_MODULES:
        return run_process(command)
    return not_implemented(command, NOT_IMPLEMENTED[command])


if __name__ == "__main__":
    raise SystemExit(main())
