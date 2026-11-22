"""Corpus helpers and fixtures for the label tests."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from lae.corpus import CORPUS_DIR, load_record
from lae.db.models import Record

MONA_LISA = "linkedart/object-0.json"
"""Primary names in English, French and Italian (``notation`` on each language)."""

NIGHT_WATCH = "linkedart/object-nightwatch-1.json"
"""Primary names in English and Dutch."""

HACHA = "linkedart/object-10.json"
"""One primary name whose languages are Spanish then English; no root ``_label``."""

SPANISH_ONLY = "linkedart/object-3.json"
"""Names in Spanish only."""

YUAG_OBJECT = "institutional/yuag-obj-31619.json"
"""Primary title in English via the AAT language id only (``_label`` "eng", no notation)."""

YCBA_PERSON = "institutional/ycba-person-85917f42-4d09-4759-8e8c-39851db1af87.json"
"""An English primary name plus names without a language."""


def corpus(name: str) -> dict[str, Any]:
    """Load one corpus record by its path below ``tests/corpus/``."""
    return load_record(CORPUS_DIR / name)


def as_record(data: dict[str, Any]) -> Record:
    """A ``records`` row object for a corpus record published under its own URI (not flushed)."""
    return Record(uri=data["id"], type=data["type"], data=data, etag="e1", context_version=1)


@pytest.fixture
def label_queries(db_engine: AsyncEngine) -> Iterator[list[str]]:
    """Collect every SQL statement that reads ``entity_labels`` while the test runs."""
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "entity_labels" in statement:
            statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", record)
