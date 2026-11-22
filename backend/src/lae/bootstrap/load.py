"""``lae bootstrap load``: stream Linked Art JSON-LD into ``records`` and ``entity_labels``.

The main process reads the input (``lae.bootstrap.source``) and hands it out in batches of
``--batch`` records (10,000) to a pool of worker processes. Each worker prepares its batch
(``lae.bootstrap.records``: id repair, JSON Schema, internal node IRIs, etag, labels) and writes
it with two binary ``COPY`` statements, ``records`` then ``entity_labels``, in one transaction on
its own connection. At most ``--streams`` workers copy at the same time (Spike S6: four streams
are 2.2x one on stock Postgres settings, eight add nothing). Rows are written as published
version 1: ``version = 1``, ``status = 'active'``, ``context_version = settings.context_version``,
and ``draft_epoch = 0`` once that column exists (migration 0004, Step 1.8; its default is 0).
Bulk-loaded records have no ``record_revisions`` row until they are first edited (spec §3.3).

**Failures.** An input item that is not a record, has an id that cannot be repaired, or fails
JSON Schema is written to the report (JSON Lines, ``LAE-BOOT-*`` codes) and skipped. So is a
record whose URI is already in ``records`` or occurs earlier in the input: ``COPY`` fails on the
unique index, and the worker then finds the duplicates, reports them, and writes the rest of the
batch, upserting the labels. With several streams, which of two equal URIs in different batches
is kept is not defined.

At the end, ``sync_state`` records ``bootstrap_context_version`` and
``bootstrap_expander_version`` (ADR-0007), and both tables are analyzed.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import sys
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from itertools import islice
from multiprocessing.synchronize import Semaphore
from pathlib import Path
from typing import Any, Final, NamedTuple

import asyncpg

from lae.bootstrap.common import (
    BOOTSTRAP_CONTEXT_VERSION,
    BOOTSTRAP_EXPANDER_VERSION,
    DUPLICATE_URI,
    Failure,
    Report,
    asyncpg_dsn,
    set_sync_state,
)
from lae.bootstrap.records import STAGE, PreparedRecord, prepare_batch
from lae.bootstrap.source import SourceItem, iter_source
from lae.db.models import EntityLabelStatus, RecordStatus
from lae.labels.upsert import LOCAL_SOURCE
from lae.rdf import EXPANDER_VERSION
from lae.settings import Settings

DEFAULT_BATCH: Final = 10_000
DEFAULT_STREAMS: Final = 4
DEFAULT_REPORT: Final = Path("bootstrap-load-report.jsonl")
BULK_VERSION: Final = 1
DUPLICATE_RETRIES: Final = 3

RECORD_COLUMNS: Final = (
    "id",
    "uri",
    "type",
    "version",
    "status",
    "etag",
    "data",
    "context_version",
)
DRAFT_EPOCH_COLUMN: Final = "draft_epoch"
LABEL_COLUMNS: Final = (
    "uri",
    "type",
    "labels",
    "default_label",
    "primary_classification",
    "source",
    "status",
)
_LABEL_UPSERT: Final = """
INSERT INTO entity_labels (uri, type, labels, default_label, primary_classification, source, status)
VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7::entity_labels_status)
ON CONFLICT (uri) DO UPDATE SET
    type = EXCLUDED.type,
    labels = EXCLUDED.labels,
    default_label = EXCLUDED.default_label,
    primary_classification = EXCLUDED.primary_classification,
    source = EXCLUDED.source,
    status = EXCLUDED.status,
    updated_at = now()
"""


class LoadedBatch(NamedTuple):
    """What a worker returns for one batch."""

    items: int
    written: int
    failures: list[Failure]
    repaired_ids: int
    prepare_cpu_s: float
    copy_s: float


class _Worker:
    """Per-process state of a load worker: its event loop, connection and settings."""

    loop: asyncio.AbstractEventLoop
    conn: asyncpg.Connection
    base_uri: str
    context_version: int
    draft_epoch: bool
    copy_slots: Semaphore


_WORKER: _Worker | None = None


def _init_worker(
    dsn: str, base_uri: str, context_version: int, draft_epoch: bool, copy_slots: Semaphore
) -> None:
    global _WORKER
    worker = _Worker()
    worker.loop = asyncio.new_event_loop()
    worker.conn = worker.loop.run_until_complete(asyncpg.connect(dsn))
    worker.base_uri = base_uri
    worker.context_version = context_version
    worker.draft_epoch = draft_epoch
    worker.copy_slots = copy_slots
    _WORKER = worker


def _record_rows(worker: _Worker, records: Iterable[PreparedRecord]) -> list[tuple[Any, ...]]:
    extra = (0,) if worker.draft_epoch else ()
    status = RecordStatus.ACTIVE.value
    return [
        (r.id, r.uri, r.type, BULK_VERSION, status, r.etag, r.data, worker.context_version, *extra)
        for r in records
    ]


def _label_rows(records: Iterable[PreparedRecord]) -> list[tuple[Any, ...]]:
    status = EntityLabelStatus.PUBLISHED.value
    return [
        (
            r.uri,
            r.type,
            r.labels,
            r.default_label,
            r.primary_classification,
            LOCAL_SOURCE,
            status,
        )
        for r in records
    ]


def _record_columns(worker: _Worker) -> tuple[str, ...]:
    return (*RECORD_COLUMNS, DRAFT_EPOCH_COLUMN) if worker.draft_epoch else RECORD_COLUMNS


async def _copy(worker: _Worker, records: list[PreparedRecord]) -> None:
    conn = worker.conn
    async with conn.transaction():
        await conn.copy_records_to_table(
            "records", records=_record_rows(worker, records), columns=_record_columns(worker)
        )
        await conn.copy_records_to_table(
            "entity_labels", records=_label_rows(records), columns=LABEL_COLUMNS
        )


async def _copy_without_duplicates(
    worker: _Worker, records: list[PreparedRecord]
) -> list[PreparedRecord]:
    """Write the records whose URI is new; upsert their labels; return the duplicates."""
    conn = worker.conn
    existing = {
        row[0]
        for row in await conn.fetch(
            "SELECT uri FROM records WHERE uri = ANY($1::text[])", [r.uri for r in records]
        )
    }
    keep: list[PreparedRecord] = []
    duplicates: list[PreparedRecord] = []
    seen: set[str] = set()
    for record in records:
        if record.uri in existing or record.uri in seen:
            duplicates.append(record)
        else:
            seen.add(record.uri)
            keep.append(record)
    async with conn.transaction():
        await conn.copy_records_to_table(
            "records", records=_record_rows(worker, keep), columns=_record_columns(worker)
        )
        await conn.executemany(_LABEL_UPSERT, _label_rows(keep))
    return duplicates


async def _write(worker: _Worker, records: list[PreparedRecord]) -> list[PreparedRecord]:
    """Write a batch; return the records skipped as duplicates."""
    if not records:
        return []
    try:
        await _copy(worker, records)
        return []
    except asyncpg.UniqueViolationError:
        pass
    # Another stream may commit one of these URIs between the lookup and the COPY: look again.
    for _ in range(DUPLICATE_RETRIES):
        try:
            return await _copy_without_duplicates(worker, records)
        except asyncpg.UniqueViolationError:
            continue
    return await _copy_without_duplicates(worker, records)


def load_batch(items: list[SourceItem]) -> LoadedBatch:
    """Worker entry point: prepare one batch and ``COPY`` it."""
    worker = _WORKER
    assert worker is not None, "load_batch runs in a pool started with _init_worker"
    prepared = prepare_batch(items, worker.base_uri)
    started = time.perf_counter()
    with worker.copy_slots:
        duplicates = worker.loop.run_until_complete(_write(worker, prepared.records))
    copy_s = time.perf_counter() - started
    failures = list(prepared.failures)
    failures.extend(
        Failure(STAGE, r.source, DUPLICATE_URI, r.uri, ["a record with this URI is already loaded"])
        for r in duplicates
    )
    return LoadedBatch(
        items=len(items),
        written=len(prepared.records) - len(duplicates),
        failures=failures,
        repaired_ids=prepared.repaired_ids,
        prepare_cpu_s=prepared.cpu_s,
        copy_s=copy_s,
    )


def _batches(items: Iterator[SourceItem], size: int) -> Iterator[list[SourceItem]]:
    while chunk := list(islice(items, size)):
        yield chunk


async def _has_column(conn: asyncpg.Connection, table: str, column: str) -> bool:
    found: bool = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = $1 AND column_name = $2)",
        table,
        column,
    )
    return found


class _Totals:
    def __init__(self) -> None:
        self.items = 0
        self.written = 0
        self.repaired_ids = 0
        self.prepare_cpu_s = 0.0
        self.copy_s = 0.0

    def add(self, batch: LoadedBatch) -> None:
        self.items += batch.items
        self.written += batch.written
        self.repaired_ids += batch.repaired_ids
        self.prepare_cpu_s += batch.prepare_cpu_s
        self.copy_s += batch.copy_s


async def load(
    input_path: Path,
    *,
    settings: Settings,
    report_path: Path = DEFAULT_REPORT,
    batch: int = DEFAULT_BATCH,
    streams: int = DEFAULT_STREAMS,
    workers: int | None = None,
    progress_every: int = 100_000,
) -> dict[str, Any]:
    """Load every record of ``input_path`` (a directory or a JSON Lines file); return a summary."""
    workers = workers or os.cpu_count() or 1
    dsn = asyncpg_dsn(settings.database_url)
    conn = await asyncpg.connect(dsn)
    try:
        draft_epoch = await _has_column(conn, "records", DRAFT_EPOCH_COLUMN)
        loop = asyncio.get_running_loop()
        context = multiprocessing.get_context("spawn")
        totals = _Totals()
        started = time.perf_counter()
        next_progress = progress_every
        with (
            Report(report_path) as report,
            ProcessPoolExecutor(
                workers,
                mp_context=context,
                initializer=_init_worker,
                initargs=(
                    dsn,
                    settings.base_uri,
                    settings.context_version,
                    draft_epoch,
                    context.Semaphore(streams),
                ),
            ) as pool,
        ):
            pending: set[asyncio.Future[LoadedBatch]] = set()

            def collect(done: set[asyncio.Future[LoadedBatch]]) -> None:
                nonlocal next_progress
                for future in done:
                    result = future.result()
                    totals.add(result)
                    report.write(result.failures)
                if progress_every and totals.items >= next_progress:
                    rate = totals.items / max(time.perf_counter() - started, 1e-9)
                    print(f"load: {totals.items} records read, {rate:.0f}/s", file=sys.stderr)
                    next_progress += progress_every

            for items in _batches(iter_source(input_path), batch):
                pending.add(loop.run_in_executor(pool, load_batch, items))
                if len(pending) >= workers + 1:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    collect(done)
            if pending:
                done, _ = await asyncio.wait(pending)
                collect(done)
            failures = dict(sorted(report.counts.items()))
        load_s = time.perf_counter() - started
        versions = {
            BOOTSTRAP_CONTEXT_VERSION: settings.context_version,
            BOOTSTRAP_EXPANDER_VERSION: EXPANDER_VERSION,
        }
        await set_sync_state(conn, versions)
        await conn.execute("ANALYZE records")
        await conn.execute("ANALYZE entity_labels")
        records_total = await conn.fetchval("SELECT count(*) FROM records")
    finally:
        await conn.close()
    return {
        "stage": STAGE,
        "input": str(input_path),
        "workers": workers,
        "streams": streams,
        "batch": batch,
        "items": totals.items,
        "records_written": totals.written,
        "records_skipped": sum(failures.values()),
        "failures": failures,
        "report": str(report_path),
        "repaired_ids": totals.repaired_ids,
        "seconds": round(load_s, 2),
        "records_per_s": round(totals.written / max(load_s, 1e-9)),
        "prepare_cpu_s": round(totals.prepare_cpu_s, 1),
        "copy_s": round(totals.copy_s, 1),
        "records_total": records_total,
        "sync_state": versions,
    }
