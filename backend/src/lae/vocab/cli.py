"""``lae vocab <import|export|refresh-membership|refresh-usage|harvest-metatypes>``.

Each command reads ``LAE_DATABASE_URL`` (and ``LAE_QLEVER_URL`` where QLever is used), runs in one
transaction, and prints a JSON summary on stdout; problems go to stderr with exit code 1.

``import [--dir DIR]``
    ``lae.vocab.loader.load_from_yaml`` on ``DIR`` (default: the repository's
    ``profiles/vocab/``), then the membership of each enumerated Value Set in it.
``export DIR``
    ``lae.vocab.loader.export_to_yaml``: one file per Value Set, plus ``_schema.json``.
``refresh-membership [ID ...]``
    ``lae.vocab.membership.refresh_membership`` for the named Value Sets, or all of them; rule
    sets use QLever when ``LAE_QLEVER_URL`` is set, else only ``entity_labels``.
``refresh-usage``
    ``lae.vocab.usage.refresh_usage_counts`` (needs QLever). The nightly ``scheduler`` job of
    Step 1.15 runs the same function.
``harvest-metatypes``
    ``lae.vocab.metatypes.harvest_all_metatypes``: adds the metatypes stated inline in every
    active record to the concepts' ``entity_labels`` rows, e.g. after a sidecar is loaded.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

import asyncpg
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lae.settings import Settings, get_settings

EXIT_FAILED: Final = 1

SUBCOMMANDS: Final[dict[str, str]] = {
    "import": "Load Value Set YAML into the registry (upsert).",
    "export": "Write the registry as Value Set YAML files.",
    "refresh-membership": "Recompute Value Set membership (rule sets through QLever).",
    "refresh-usage": "Recount entity_labels.usage_count from QLever.",
    "harvest-metatypes": "Add metatypes stated inline in records to entity_labels.",
}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the ``vocab`` sub-commands and their options to ``parser``."""
    from lae.vocab.loader import VOCAB_DIR

    sub = parser.add_subparsers(dest="action", required=True, metavar="<action>")
    imp = sub.add_parser("import", help=SUBCOMMANDS["import"], description=SUBCOMMANDS["import"])
    imp.add_argument(
        "--dir", type=Path, default=VOCAB_DIR, help=f"Value Set files (default: {VOCAB_DIR})"
    )
    exp = sub.add_parser("export", help=SUBCOMMANDS["export"], description=SUBCOMMANDS["export"])
    exp.add_argument("dir", type=Path, help="output directory (created if missing)")
    refresh = sub.add_parser(
        "refresh-membership",
        help=SUBCOMMANDS["refresh-membership"],
        description=SUBCOMMANDS["refresh-membership"],
    )
    refresh.add_argument("ids", nargs="*", metavar="ID", help="Value Set ids (default: all)")
    for name in ("refresh-usage", "harvest-metatypes"):
        sub.add_parser(name, help=SUBCOMMANDS[name], description=SUBCOMMANDS[name])


class CommandError(RuntimeError):
    """A vocab command could not run; the message is printed on stderr."""


async def _in_session(
    settings: Settings, work: Callable[[AsyncSession], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    """Run ``work`` in one session and transaction, commit, and return its summary."""
    from lae.db.session import create_engine, create_sessionmaker

    engine = create_engine(settings.database_url)
    try:
        async with create_sessionmaker(engine)() as session:
            summary = await work(session)
            await session.commit()
            return summary
    finally:
        await engine.dispose()


def _qlever(settings: Settings) -> Any:
    from lae.sync.qlever_client import QLeverClient

    if not settings.qlever_url:
        return None
    token = settings.qlever_access_token
    return QLeverClient(
        settings.qlever_url,
        settings.qlever_update_url,
        token.get_secret_value() if token is not None else None,
    )


async def run_import(settings: Settings, directory: Path) -> dict[str, Any]:
    """``lae vocab import``."""
    from lae.vocab.loader import load_from_yaml

    async def work(session: AsyncSession) -> dict[str, Any]:
        summary = await load_from_yaml(session, directory)
        return {"action": "import", "dir": str(directory), **summary.as_json()}

    return await _in_session(settings, work)


async def run_export(settings: Settings, directory: Path) -> dict[str, Any]:
    """``lae vocab export``."""
    from lae.vocab.loader import export_to_yaml

    async def work(session: AsyncSession) -> dict[str, Any]:
        files = await export_to_yaml(session, directory)
        return {"action": "export", "dir": str(directory), "files": len(files)}

    return await _in_session(settings, work)


async def run_refresh_membership(settings: Settings, ids: list[str]) -> dict[str, Any]:
    """``lae vocab refresh-membership``."""
    from lae.db.models import Vocabulary
    from lae.vocab.membership import refresh_membership

    qlever = _qlever(settings)

    async def work(session: AsyncSession) -> dict[str, Any]:
        wanted = ids or list(await session.scalars(select(Vocabulary.id).order_by(Vocabulary.id)))
        counts = {}
        for vocabulary_id in wanted:
            counts[vocabulary_id] = await refresh_membership(
                session, vocabulary_id, qlever=qlever, context_version=settings.context_version
            )
        return {"action": "refresh-membership", "qlever": qlever is not None, "members": counts}

    try:
        return await _in_session(settings, work)
    finally:
        if qlever is not None:
            await qlever.aclose()


async def run_refresh_usage(settings: Settings) -> dict[str, Any]:
    """``lae vocab refresh-usage``."""
    from lae.vocab.usage import refresh_usage_counts

    qlever = _qlever(settings)
    if qlever is None:
        raise CommandError("LAE_QLEVER_URL is not set; usage counts come from QLever")

    async def work(session: AsyncSession) -> dict[str, Any]:
        summary = await refresh_usage_counts(
            session, qlever, context_version=settings.context_version
        )
        return {"action": "refresh-usage", **summary.as_json()}

    try:
        return await _in_session(settings, work)
    finally:
        await qlever.aclose()


async def run_harvest(settings: Settings) -> dict[str, Any]:
    """``lae vocab harvest-metatypes``."""
    from lae.bootstrap.common import asyncpg_dsn
    from lae.vocab.metatypes import harvest_all_metatypes

    conn = await asyncpg.connect(asyncpg_dsn(settings.database_url))
    try:
        async with conn.transaction():
            changed = await harvest_all_metatypes(conn)
    finally:
        await conn.close()
    return {"action": "harvest-metatypes", "rows_changed": changed}


def _dispatch(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    action: str = args.action
    if action == "import":
        return asyncio.run(run_import(settings, args.dir))
    if action == "export":
        return asyncio.run(run_export(settings, args.dir))
    if action == "refresh-membership":
        return asyncio.run(run_refresh_membership(settings, list(args.ids)))
    if action == "refresh-usage":
        return asyncio.run(run_refresh_usage(settings))
    if action == "harvest-metatypes":
        return asyncio.run(run_harvest(settings))
    raise AssertionError(f"unknown vocab action {action!r}")


def run(args: argparse.Namespace) -> int:
    """Run the selected action and print its JSON summary; return the exit code."""
    from lae.bootstrap.common import print_summary
    from lae.sync.qlever_client import QLeverError
    from lae.vocab.loader import VocabConfigError
    from lae.vocab.membership import UnknownVocabularyError

    try:
        summary = _dispatch(args, get_settings())
    except VocabConfigError as error:
        print(f"lae vocab {args.action}: {error}", file=sys.stderr)
        return EXIT_FAILED
    except UnknownVocabularyError as error:
        print(
            f"lae vocab {args.action}: unknown Value Set {error.vocabulary_id!r}", file=sys.stderr
        )
        return EXIT_FAILED
    except (CommandError, QLeverError) as error:
        print(f"lae vocab {args.action}: {error}", file=sys.stderr)
        return EXIT_FAILED
    print_summary(summary)
    return 0
