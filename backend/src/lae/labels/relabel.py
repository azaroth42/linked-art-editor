"""``lae relabel``: recompute ``entity_labels.default_label`` under the label language (Step 1.19b).

Run by the administrator after ``LAE_LABEL_LANGUAGE`` was changed and ``api`` and ``crdt-ws``
were restarted (``deploy/README.md``). Contract ``docs/contracts/1.19b-relabel.md``; ADR-0038.

**What a row becomes.** A ``local`` row takes ``labels_from_record(records.data, L)[1]`` of the
record with its URI, whatever the record's status: exactly what publish writes. Without a record
the row is left alone (``no_record``). Any other row takes ``default_label_from_labels`` of its
own labels with its source's language (``lae.labels.defaults``); ``None`` leaves it alone
(``kept``). A row is written only when the label is a different string.

**What it writes.** One column, ``entity_labels.default_label``, through one raw ``UPDATE``
(no ORM: ``EntityLabel.updated_at`` has ``onupdate``, and ``updated_at`` must not move, or every
relabelled row would enter the autocomplete overlay). ``records``, the revisions, the outbox, the
drafts, QLever and ``crdt-ws`` are not touched: published records keep their stored ``_label``
until they are next published. At the end of a run that is not a dry run, one ``vocabularies``
row's ``updated_at`` is moved so that every ``api`` process rebuilds its vocabulary bundle.

**Locking.** One batch: page the URIs (no transaction); in a READ COMMITTED transaction lock
their ``entity_labels`` rows with ``FOR UPDATE SKIP LOCKED`` (``_lock``), then, in a statement of
its own, read the records of the locked local rows (``_records``), compute, write (``_apply``),
commit. The record is read only after its row lock is held, so a publish of it either committed
before (and the read sees its data) or writes its own label after the batch. The run never waits
for a row lock and never locks ``records``; it is outside the publish lock order. Rows it could
not lock are retried after a pause, up to ``retry_passes`` times; a row still locked then is
``skipped_locked`` (the command exits 1), one that disappeared is ``gone``.
"""

from __future__ import annotations

import asyncio
import getpass
import time
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any, Final

import asyncpg
import orjson

from lae.bootstrap.common import asyncpg_dsn
from lae.labels.defaults import SOURCE_LANGUAGES, default_label_from_labels
from lae.labels.extract import labels_from_record
from lae.labels.upsert import LOCAL_SOURCE
from lae.logging import get_logger

RELABEL_BATCH: Final = 500
"""URIs per batch (and per transaction)."""
RETRY_PASSES: Final = 5
"""Passes over the rows another transaction held, after the last page."""
RETRY_PAUSE_SECONDS: Final = 2.0
"""Pause before each retry pass."""
PROGRESS_EVERY: Final = 10
"""``relabel_progress`` is logged after every this many batches."""

_PAGE: Final = "SELECT uri FROM entity_labels WHERE uri > $1 ORDER BY uri LIMIT $2"
_ROWS: Final = (
    "SELECT uri, source, labels::text AS labels, default_label "
    "FROM entity_labels WHERE uri = ANY($1::text[])"
)
_LOCK: Final = _ROWS + " FOR UPDATE SKIP LOCKED"
_RECORDS: Final = "SELECT uri, data::text AS data FROM records WHERE uri = ANY($1::text[])"
_APPLY: Final = (
    "UPDATE entity_labels AS e SET default_label = v.label "
    "FROM unnest($1::text[], $2::text[]) AS v(uri, label) WHERE e.uri = v.uri"
)
_PRESENT: Final = "SELECT uri FROM entity_labels WHERE uri = ANY($1::text[])"
_TOUCH_REGISTRY: Final = (
    "UPDATE vocabularies SET updated_at = clock_timestamp() "
    "WHERE id = (SELECT id FROM vocabularies ORDER BY id LIMIT 1)"
)

LOGGER_NAME: Final = "lae.labels.relabel"
"""The logger of the run: until ``admin_audit_log`` exists (Step 2.17) it is the audit trail."""


# --- the four steps of one batch (tests park a run between them) ---------------------------------


async def _page(conn: asyncpg.Connection, after: str, limit: int) -> list[str]:
    """The next ``limit`` URIs of ``entity_labels`` after ``after``, in index order (no lock)."""
    rows = await conn.fetch(_PAGE, after, limit)
    return [row["uri"] for row in rows]


async def _lock(
    conn: asyncpg.Connection, uris: Sequence[str], *, for_update: bool = True
) -> list[asyncpg.Record]:
    """Lock and read the rows of ``uris`` that no other transaction holds.

    ``for_update=False`` (a dry run) reads them without the locking clause.
    """
    rows: list[asyncpg.Record] = await conn.fetch(_LOCK if for_update else _ROWS, list(uris))
    return rows


async def _records(conn: asyncpg.Connection, uris: Sequence[str]) -> dict[str, str]:
    """``uri → records.data`` (JSON text, whole) of the records of ``uris``.

    A statement of its own, sent after ``_lock`` has returned.
    """
    if not uris:
        return {}
    rows = await conn.fetch(_RECORDS, list(uris))
    return {row["uri"]: row["data"] for row in rows}


async def _apply(conn: asyncpg.Connection, changes: Sequence[tuple[str, str]]) -> None:
    """Set ``default_label`` of each ``(uri, label)``: the only write to ``entity_labels``."""
    if changes:
        await conn.execute(_APPLY, [uri for uri, _ in changes], [label for _, label in changes])


# --- computing ---------------------------------------------------------------------------------


class _Tally:
    """The counts of one run."""

    def __init__(self) -> None:
        self.rows = 0
        self.changed: dict[str, int] = {}
        self.unchanged = 0
        self.no_record = 0
        self.kept = 0
        self.retried = 0
        self.skipped_locked = 0
        self.gone = 0

    @property
    def changed_total(self) -> int:
        return sum(self.changed.values())


class _Outcome:
    """What one batch found: its changes and how each row it obtained was counted."""

    def __init__(self) -> None:
        self.changes: list[tuple[str, str]] = []
        self.lines: list[dict[str, Any]] = []
        self.changed: dict[str, int] = {}
        self.unchanged = 0
        self.no_record = 0
        self.kept = 0

    def merge_into(self, tally: _Tally) -> None:
        for source, count in self.changed.items():
            tally.changed[source] = tally.changed.get(source, 0) + count
        tally.unchanged += self.unchanged
        tally.no_record += self.no_record
        tally.kept += self.kept


def _new_label(
    row: asyncpg.Record, records: dict[str, str], label_language: str
) -> tuple[str | None, bool]:
    """``(new default label or None, whether the row is local without a record)``."""
    source: str = row["source"]
    if source == LOCAL_SOURCE:
        data = records.get(row["uri"])
        if data is None:
            return None, True
        return labels_from_record(orjson.loads(data), label_language)[1], False
    labels = orjson.loads(row["labels"])
    if not isinstance(labels, dict):
        labels = {}
    return default_label_from_labels(labels, label_language, SOURCE_LANGUAGES.get(source)), False


def _compute(
    rows: Sequence[asyncpg.Record], records: dict[str, str], label_language: str
) -> _Outcome:
    outcome = _Outcome()
    for row in rows:
        new, no_record = _new_label(row, records, label_language)
        if no_record:
            outcome.no_record += 1
        elif new is None:
            outcome.kept += 1
        elif new == row["default_label"]:
            outcome.unchanged += 1
        else:
            source = row["source"]
            outcome.changes.append((row["uri"], new))
            outcome.changed[source] = outcome.changed.get(source, 0) + 1
            outcome.lines.append(
                {"uri": row["uri"], "source": source, "old": row["default_label"], "new": new}
            )
    return outcome


def _local_uris(rows: Sequence[asyncpg.Record]) -> list[str]:
    return [row["uri"] for row in rows if row["source"] == LOCAL_SOURCE]


async def _batch(
    conn: asyncpg.Connection, uris: Sequence[str], label_language: str, *, dry_run: bool
) -> tuple[_Outcome, list[str]]:
    """Relabel the rows of ``uris``; return the outcome and the URIs not obtained (deferred).

    Nothing but the database is awaited between ``_lock`` and the commit; the report lines are
    written by the caller after it.
    """
    if dry_run:
        rows = await _lock(conn, uris, for_update=False)
        records = await _records(conn, _local_uris(rows))
        outcome = _compute(rows, records, label_language)
    else:
        async with conn.transaction(isolation="read_committed"):
            rows = await _lock(conn, uris)
            records = await _records(conn, _local_uris(rows))
            outcome = _compute(rows, records, label_language)
            await _apply(conn, outcome.changes)
    obtained = {row["uri"] for row in rows}
    return outcome, [uri for uri in uris if uri not in obtained]


def _chunks(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[start : start + size] for start in range(0, len(items), size)]


def _operator() -> str | None:
    try:
        return getpass.getuser()
    except Exception:  # no user name in the environment or the password database
        return None


def _write_lines(report: IO[bytes] | None, lines: list[dict[str, Any]]) -> None:
    if report is not None and lines:
        report.write(b"".join(orjson.dumps(line) + b"\n" for line in lines))
        report.flush()


async def relabel(
    database_url: str,
    label_language: str,
    *,
    dry_run: bool = False,
    batch: int = RELABEL_BATCH,
    report_path: Path | None = None,
    retry_passes: int = RETRY_PASSES,
    retry_pause: float = RETRY_PAUSE_SECONDS,
) -> dict[str, Any]:
    """Recompute ``entity_labels.default_label`` under ``label_language``; return the summary.

    ``label_language`` is ``Settings.label_language``. With ``dry_run`` nothing is written, no
    lock is taken and the registry is not touched; the counts and report lines are those a real
    run would give on the same data. ``report_path`` receives one JSON line per row changed.
    """
    if batch < 1:
        raise ValueError("batch must be at least 1")
    started = time.perf_counter()
    operator = _operator()
    get_logger(LOGGER_NAME).info(
        "relabel_started",
        label_language=label_language,
        dry_run=dry_run,
        batch=batch,
        operator=operator,
    )
    tally = _Tally()
    try:
        report = report_path.open("wb") if report_path is not None else None
        try:
            conn = await asyncpg.connect(asyncpg_dsn(database_url))
            try:
                registry_touched = await _run(
                    conn,
                    label_language,
                    tally,
                    report,
                    dry_run=dry_run,
                    batch=batch,
                    retry_passes=retry_passes,
                    retry_pause=retry_pause,
                )
            finally:
                await conn.close()
        finally:
            if report is not None:
                report.close()
    except BaseException as error:
        get_logger(LOGGER_NAME).error(
            "relabel_failed", error=type(error).__name__, operator=operator
        )
        raise
    summary: dict[str, Any] = {
        "command": "relabel",
        "label_language": label_language,
        "dry_run": dry_run,
        "batch": batch,
        "rows": tally.rows,
        "changed": dict(sorted(tally.changed.items())),
        "changed_total": tally.changed_total,
        "unchanged": tally.unchanged,
        "no_record": tally.no_record,
        "kept": tally.kept,
        "retried": tally.retried,
        "skipped_locked": tally.skipped_locked,
        "gone": tally.gone,
        "registry_touched": registry_touched,
        "report": str(report_path) if report_path is not None else None,
        "seconds": round(time.perf_counter() - started, 2),
    }
    get_logger(LOGGER_NAME).info("relabel_finished", **summary, operator=operator)
    return summary


async def _run(
    conn: asyncpg.Connection,
    label_language: str,
    tally: _Tally,
    report: IO[bytes] | None,
    *,
    dry_run: bool,
    batch: int,
    retry_passes: int,
    retry_pause: float,
) -> bool:
    """Page, relabel, retry the deferred rows, touch the registry; return whether it was."""
    deferred: list[str] = []
    after = ""
    batches = 0
    while True:
        uris = await _page(conn, after, batch)
        if not uris:
            break
        tally.rows += len(uris)
        after = uris[-1]
        outcome, missed = await _batch(conn, uris, label_language, dry_run=dry_run)
        outcome.merge_into(tally)
        _write_lines(report, outcome.lines)
        if dry_run:
            tally.gone += len(missed)  # deleted between the page and the read
        else:
            deferred.extend(missed)
        batches += 1
        if batches % PROGRESS_EVERY == 0:
            _progress(tally, deferred, after)

    for _ in range(retry_passes):
        if not deferred:
            break
        await asyncio.sleep(retry_pause)
        still: list[str] = []
        for chunk in _chunks(deferred, batch):
            outcome, missed = await _batch(conn, chunk, label_language, dry_run=False)
            outcome.merge_into(tally)
            _write_lines(report, outcome.lines)
            tally.retried += len(chunk) - len(missed)
            still.extend(missed)
        deferred = still
        _progress(tally, deferred, after)

    if deferred:
        present = {row["uri"] for row in await conn.fetch(_PRESENT, deferred)}
        tally.skipped_locked += len(present)
        tally.gone += len(deferred) - len(present)

    if dry_run:
        return False
    async with conn.transaction():
        status = await conn.execute(_TOUCH_REGISTRY)
    return bool(status != "UPDATE 0")


def _progress(tally: _Tally, deferred: Sequence[str], last_uri: str) -> None:
    get_logger(LOGGER_NAME).info(
        "relabel_progress",
        rows=tally.rows,
        changed_total=tally.changed_total,
        deferred=len(deferred),
        last_uri=last_uri,
    )
