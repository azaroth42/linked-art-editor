"""Pieces shared by the bootstrap commands: database access, the failure report, ``sync_state``."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Final, Self

import asyncpg
from sqlalchemy.engine import make_url

# Diagnostic codes of the bootstrap report (conventions §2). Each report line carries one.
NOT_A_RECORD: Final = "LAE-BOOT-001"
"""The input is not a JSON object (or not valid JSON)."""
NO_RECORD_URI: Final = "LAE-BOOT-002"
"""The record has no root ``id``, or its root ``id`` cannot be made a valid IRI."""
INVALID_ID: Final = "LAE-BOOT-003"
"""An ``id`` below the root cannot be made a valid IRI."""
UNKNOWN_TYPE: Final = "LAE-BOOT-004"
"""The root ``type`` is not a Linked Art record class (no JSON Schema for it)."""
SCHEMA_INVALID: Final = "LAE-BOOT-005"
"""The record does not validate against the JSON Schema for its type."""
DUPLICATE_URI: Final = "LAE-BOOT-006"
"""A record with this URI is already loaded, or appeared earlier in the input."""
EXPANSION_FAILED: Final = "LAE-BOOT-007"
"""``lae.rdf.expand_to_ntriples`` raised ``ExpansionError`` (export)."""
SIDECAR_LABEL_INVALID: Final = "LAE-BOOT-008"
"""A sidecar ``entity_labels`` row has a URI, type or source that cannot be written as RDF."""
CSV_ROW_INVALID: Final = "LAE-BOOT-009"
"""An AAT CSV row has no numeric ``aat_code`` or no ``preferred_name``, or repeats a code."""

QLEVER_INDEX_WATERMARK: Final = "qlever_index_watermark"
"""``sync_state`` key: id of the last outbox event reflected in the live QLever index."""
QLEVER_INDEX_WATERMARK_TIME: Final = "qlever_index_watermark_time"
"""``sync_state`` key: when the snapshot the live index was built from was taken (ISO 8601 with
offset, as a JSON string; ``null`` if unknown). Rows of ``entity_labels`` updated later are not in
QLever's text index, so autocomplete searches them in Postgres (Step 1.13)."""
BOOTSTRAP_CONTEXT_VERSION: Final = "bootstrap_context_version"
"""``sync_state`` key: context version of the bulk load (ADR-0007)."""
BOOTSTRAP_EXPANDER_VERSION: Final = "bootstrap_expander_version"
"""``sync_state`` key: ``lae.rdf.EXPANDER_VERSION`` of the bulk load (ADR-0007)."""


def asyncpg_dsn(database_url: str) -> str:
    """Turn an SQLAlchemy URL (``postgresql+asyncpg://…``) into a DSN ``asyncpg`` accepts."""
    url = make_url(database_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


async def set_sync_state(conn: asyncpg.Connection, values: dict[str, Any]) -> None:
    """Upsert ``sync_state`` rows; each value is stored as JSON."""
    await conn.executemany(
        "INSERT INTO sync_state (key, value) VALUES ($1, $2::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        [(key, json.dumps(value)) for key, value in values.items()],
    )


async def get_sync_state(conn: asyncpg.Connection, key: str) -> Any:
    """Return the JSON value of one ``sync_state`` row, or ``None`` if there is none."""
    raw = await conn.fetchval("SELECT value::text FROM sync_state WHERE key = $1", key)
    return None if raw is None else json.loads(raw)


@dataclass(frozen=True, slots=True)
class Failure:
    """One input item that a bootstrap stage skipped, and why."""

    stage: str
    """``load``, ``export`` or ``aat-labels``."""
    source: str
    """Where the item came from: ``<file>``, ``<file>:<line>``, or a record URI."""
    code: str
    """One of the ``LAE-BOOT-*`` codes of this module."""
    uri: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        """The report line for this failure."""
        return {
            "stage": self.stage,
            "source": self.source,
            "uri": self.uri,
            "code": self.code,
            "errors": self.errors,
        }


class Report:
    """A JSON Lines file of ``Failure`` entries, opened lazily and counted by code."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.counts: dict[str, int] = {}
        self._file: IO[str] | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def write(self, failures: Iterable[Failure]) -> None:
        """Append failures to the report."""
        assert self._file is not None, "use Report as a context manager"
        for failure in failures:
            self.counts[failure.code] = self.counts.get(failure.code, 0) + 1
            self._file.write(json.dumps(failure.as_json(), ensure_ascii=False) + "\n")

    @property
    def total(self) -> int:
        """Number of failures written."""
        return sum(self.counts.values())


def print_summary(summary: dict[str, Any]) -> None:
    """Print a stage summary as indented JSON on stdout."""
    print(json.dumps(summary, indent=2, ensure_ascii=False))
