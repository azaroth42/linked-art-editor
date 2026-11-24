"""Helpers for the ``lae relabel`` tests (Step 1.19b).

``relabel`` visits every ``entity_labels`` row of the database, so these tests start from empty
tables (``clean_tables``) instead of counting only their own rows; under pytest-xdist each worker
has its own database, and the tests of one worker run one after another. Tables are compared as
whole-row dumps (``to_jsonb`` of every row).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import asyncpg
import orjson
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from lae.bootstrap.common import asyncpg_dsn
from lae.bootstrap.load import load
from lae.corpus import CORPUS_DIR
from lae.labels.relabel import relabel
from lae.settings import Settings
from tests.vocab.conftest import empty_registry, import_seeds

DATA_TABLES = (
    "records, record_aliases, record_revisions, qa_findings, entity_labels, sync_state, "
    "outbox_events, crdt_drafts, crdt_updates"
)
"""Emptied before and after each relabel test (with the vocabulary registry)."""

UNTOUCHED_TABLES = (
    "records",
    "record_revisions",
    "outbox_events",
    "crdt_drafts",
    "crdt_updates",
    "qa_findings",
    "sync_state",
    "record_aliases",
    "vocabulary_terms",
    "vocabulary_membership",
    "slot_vocabulary_bindings",
)
"""Contract §3 invariant 3: byte for byte as before a run."""

RUN_TIMEOUT = 60.0
"""Seconds a test run of ``relabel`` may take (the corpus takes about one)."""

BASE_URI = "https://editor.example.org/id/"
AAT = "http://vocab.getty.edu/aat/"


async def execute(db_url: str, sql: str, *args: Any) -> str:
    conn = await asyncpg.connect(asyncpg_dsn(db_url))
    try:
        status: str = await conn.execute(sql, *args)
        return status
    finally:
        await conn.close()


async def fetch(db_url: str, sql: str, *args: Any) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(asyncpg_dsn(db_url))
    try:
        rows: list[asyncpg.Record] = await conn.fetch(sql, *args)
        return rows
    finally:
        await conn.close()


async def empty_tables(db_url: str, engine: AsyncEngine) -> None:
    await execute(db_url, f"TRUNCATE {DATA_TABLES} RESTART IDENTITY CASCADE")
    await empty_registry(engine)


async def dump(db_url: str, table: str) -> list[str]:
    """Every row of ``table`` as JSON text, sorted."""
    rows = await fetch(db_url, f"SELECT to_jsonb(t)::text AS row FROM {table} t")
    return sorted(row["row"] for row in rows)


async def dump_all(db_url: str, tables: Iterable[str] = UNTOUCHED_TABLES) -> dict[str, list[str]]:
    return {table: await dump(db_url, table) for table in tables}


async def label_rows(db_url: str) -> dict[str, dict[str, Any]]:
    """``uri → every column`` of ``entity_labels`` (timestamps as text, exact)."""
    rows = await fetch(db_url, "SELECT uri, to_jsonb(e)::text AS row FROM entity_labels e")
    return {row["uri"]: orjson.loads(row["row"]) for row in rows}


async def defaults(db_url: str) -> dict[str, str]:
    rows = await fetch(db_url, "SELECT uri, default_label FROM entity_labels")
    return {row["uri"]: row["default_label"] for row in rows}


async def run(db_url: str, label_language: str, **options: Any) -> dict[str, Any]:
    """``relabel`` with no pause between retry passes (contract §5).

    Bounded by ``RUN_TIMEOUT``: a run that waits for a lock or pages forever fails the test
    instead of hanging it.
    """
    options.setdefault("retry_pause", 0)
    return await asyncio.wait_for(relabel(db_url, label_language, **options), RUN_TIMEOUT)


def settings_for(db_url: str, label_language: str = "en") -> Settings:
    return Settings(
        database_url=db_url, base_uri=BASE_URI, context_version=1, label_language=label_language
    )


async def load_corpus(db_url: str, label_language: str, tmp_path: Path) -> dict[str, Any]:
    """Bulk-load every corpus record under ``label_language``."""
    return await load(
        CORPUS_DIR,
        settings=settings_for(db_url, label_language),
        report_path=tmp_path / f"load-{label_language}.jsonl",
        workers=1,
    )


async def put_label(
    db_url: str,
    uri: str,
    labels: dict[str, Any],
    default_label: str,
    *,
    source: str = "aat",
    type_name: str = "Type",
) -> None:
    await execute(
        db_url,
        "INSERT INTO entity_labels (uri, type, labels, default_label, source, status) "
        "VALUES ($1, $2, $3::jsonb, $4, $5, 'published')",
        uri,
        type_name,
        orjson.dumps(labels).decode(),
        default_label,
        source,
    )


async def put_record(db_url: str, uri: str, data: dict[str, Any]) -> None:
    """A ``records`` row for ``data`` (no ``entity_labels`` row)."""
    await execute(
        db_url,
        "INSERT INTO records (id, uri, type, data, etag, context_version) "
        "VALUES ($1, $2, $3, $4::jsonb, 'e', 1)",
        uuid7(),
        uri,
        data.get("type", "HumanMadeObject"),
        orjson.dumps(data).decode(),
    )


async def import_registry(engine: AsyncEngine) -> None:
    await import_seeds(engine)


def report_lines(path: Path) -> list[dict[str, Any]]:
    return [orjson.loads(line) for line in path.read_bytes().splitlines() if line.strip()]
