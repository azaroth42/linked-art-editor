"""Fixtures for the bootstrap tests.

The bootstrap writes through its own connections (and from worker processes), so these tests
cannot use the rolled-back ``db_session``. ``bootstrap_db`` empties the tables the bootstrap
writes before and after each test instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from lae.bootstrap.common import asyncpg_dsn
from lae.corpus import CORPUS_DIR, load_record
from lae.settings import Settings

TABLES = "records, entity_labels, sync_state, outbox_events"
BASE_URI = "https://editor.example.org/id/"

BROWN_TEA_POT = "institutional/ycba-object-5497f1dd-415f-4c05-b593-2a87e00d2ee2.json"
"""Validates; an English primary name; classified as painting (aat:300033618)."""
NIGHT_WATCH = "linkedart/object-nightwatch-1.json"
"""Validates; primary names in English and Dutch."""
YCBA_PERSON = "institutional/ycba-person-85917f42-4d09-4759-8e8c-39851db1af87.json"
"""An institutional record that validates."""
YUAG_OBJECT = "institutional/yuag-obj-31619.json"
"""Fails JSON Schema: ``used_for`` is an object (tests/corpus/KNOWN_FAILURES.md)."""


def corpus(name: str) -> dict[str, Any]:
    """Load one corpus record by its path below ``tests/corpus/``."""
    return load_record(CORPUS_DIR / name)


def corpus_path(name: str) -> Path:
    """The path of one corpus record."""
    return CORPUS_DIR / name


async def _truncate(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    finally:
        await conn.close()


@pytest.fixture
def bootstrap_settings(db_url: str) -> Iterator[Settings]:
    """Settings on the (emptied) test database; the tables are emptied again afterwards."""
    dsn = asyncpg_dsn(db_url)
    asyncio.run(_truncate(dsn))
    yield Settings(database_url=db_url, base_uri=BASE_URI, context_version=1)
    asyncio.run(_truncate(dsn))


async def fetch(settings: Settings, sql: str, *args: Any) -> list[asyncpg.Record]:
    """Run one query on the test database."""
    conn = await asyncpg.connect(asyncpg_dsn(settings.database_url))
    try:
        rows: list[asyncpg.Record] = await conn.fetch(sql, *args)
        return rows
    finally:
        await conn.close()
