"""``lae bootstrap load``: stream Linked Art JSON-LD into ``records`` and ``entity_labels``.

A load has two passes, both in one pool of worker processes (ADR-0030).

**Pass 1: load.** The main process reads the input (``lae.bootstrap.source``) and hands it out
in batches of ``--batch`` records (10,000). Each worker parses its batch, drops the items whose
source URI is already loaded, and prepares the rest (``lae.bootstrap.records``): id repair,
re-minting to ``<base_uri><uuidv7>``, internal node IRIs, the RDF expansion check, JSON Schema,
etag, labels. It then writes the batch in one transaction on its own connection with binary
``COPY``: ``records``, ``entity_labels``, ``record_aliases`` (the source URI of every re-minted
record) and ``qa_findings`` (the schema failures of records loaded anyway). At most ``--streams``
workers write at the same time (Spike S6: four streams are 2.2x one on stock Postgres settings,
eight add nothing). Rows are written as published version 1: ``version = 1``,
``status = 'active'``, ``context_version = settings.context_version``, and ``draft_epoch = 0``
once that column exists (Step 1.8; its default is 0). Bulk-loaded records have
no ``record_revisions`` row until they are first edited (spec §3.3).

**Pass 2: rewrite and normalize** (``lae.bootstrap.rewrite``). Every reference to a re-minted
record is rewritten to its new URI, and ``_label`` is stripped from references whose target has
an ``entity_labels`` row. It runs over the rows created since the load began, or since an
earlier load that did not finish.

**What is not loaded.** An input item is written to the report (JSON Lines, ``LAE-BOOT-*``
codes) and skipped when it is not a record (001), has an id that cannot be repaired (002, 003),
is of no Linked Art record class (004), cannot be expanded to RDF (007), or fails in a way
nothing else covers (010). So is a record that is already loaded (006): its source URI is a
``records.uri`` or a ``record_aliases.alias_uri``, or that of a record loaded earlier from the
input (an earlier item that was rejected does not count). Loading the
same input again therefore writes nothing. Two equal source URIs in batches that two workers
write at the same moment are caught by the unique indexes (``COPY`` fails, and the worker looks
again and writes the rest); which of the two is kept is not defined.

A record that fails JSON Schema *is* loaded; the summary counts such records and their findings.

**One load at a time.** The main connection holds an advisory lock for the whole run; a second
``load`` (or ``rewrite-refs``) on the same database stops at once. Memory is bounded by the
batches in flight: the alias map is never held in memory, pass 2 looks aliases up per batch.

At the end, ``sync_state`` records ``bootstrap_context_version`` and
``bootstrap_expander_version`` (ADR-0007), the metatypes that records state inline on the concepts
they reference are added to those concepts' ``entity_labels`` rows
(``lae.vocab.metatypes.harvest_all_metatypes``, ADR-0032; after pass 2, so under the final
URIs), and the tables are vacuumed and analyzed.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import sys
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any, Final, NamedTuple

import asyncpg

from lae.aliases import BOOTSTRAP_SOURCE
from lae.bootstrap.common import (
    BOOTSTRAP_CONTEXT_VERSION,
    BOOTSTRAP_EXPANDER_VERSION,
    BOOTSTRAP_REWRITE_SINCE,
    DUPLICATE_URI,
    Failure,
    Report,
    asyncpg_dsn,
    get_sync_state,
    set_sync_state,
)
from lae.bootstrap.records import (
    STAGE,
    ParsedItem,
    PreparedRecord,
    build_batch,
    parse_batch,
)
from lae.bootstrap.rewrite import (
    acquire_load_lock,
    finish,
    has_column,
    index_is_stale,
    rewrite_pass,
)
from lae.bootstrap.source import SourceItem, iter_source
from lae.bootstrap.worker import Worker, current, init_worker
from lae.db.models import EntityLabelStatus, QaFindingSeverity, RecordStatus
from lae.ids import new_uuid7
from lae.labels.upsert import LOCAL_SOURCE
from lae.rdf import EXPANDER_VERSION
from lae.settings import Settings
from lae.vocab.metatypes import harvest_all_metatypes

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
ALIAS_COLUMNS: Final = ("alias_uri", "record_id", "source")
FINDING_COLUMNS: Final = ("id", "code", "severity", "record_id", "params", "sweep_run_id")

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
_LOADED: Final = """
SELECT alias_uri FROM record_aliases WHERE alias_uri = ANY($1::text[])
UNION ALL
SELECT uri FROM records WHERE uri = ANY($1::text[])
"""


class LoadedBatch(NamedTuple):
    """What a worker returns for one batch."""

    items: int
    written: int
    reminted: int
    flagged: int
    findings: dict[str, int]
    failures: list[Failure]
    repaired_ids: int
    prepare_cpu_s: float
    copy_s: float


def _record_rows(worker: Worker, records: Iterable[PreparedRecord]) -> list[tuple[Any, ...]]:
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


def _alias_rows(records: Iterable[PreparedRecord]) -> list[tuple[Any, ...]]:
    return [(r.alias, r.id, BOOTSTRAP_SOURCE) for r in records if r.alias is not None]


def _finding_rows(worker: Worker, records: Iterable[PreparedRecord]) -> list[tuple[Any, ...]]:
    severity = QaFindingSeverity.ERROR.value
    return [
        (new_uuid7(), code, severity, r.id, params, worker.run_id)
        for r in records
        for code, params in r.findings
    ]


def _record_columns(worker: Worker) -> tuple[str, ...]:
    return (*RECORD_COLUMNS, DRAFT_EPOCH_COLUMN) if worker.draft_epoch else RECORD_COLUMNS


async def _copy(worker: Worker, records: list[PreparedRecord], *, upsert_labels: bool) -> None:
    """Write one batch in one transaction; ``UniqueViolationError`` leaves nothing behind."""
    conn = worker.conn
    async with conn.transaction():
        await conn.copy_records_to_table(
            "records", records=_record_rows(worker, records), columns=_record_columns(worker)
        )
        if upsert_labels:
            await conn.executemany(_LABEL_UPSERT, _label_rows(records))
        else:
            await conn.copy_records_to_table(
                "entity_labels", records=_label_rows(records), columns=LABEL_COLUMNS
            )
        aliases = _alias_rows(records)
        if aliases:
            await conn.copy_records_to_table(
                "record_aliases", records=aliases, columns=ALIAS_COLUMNS
            )
        findings = _finding_rows(worker, records)
        if findings:
            await conn.copy_records_to_table(
                "qa_findings", records=findings, columns=FINDING_COLUMNS
            )


async def _loaded(conn: asyncpg.Connection, source_uris: list[str]) -> set[str]:
    """Return those of ``source_uris`` that are already a record URI or an alias."""
    if not source_uris:
        return set()
    return {row[0] for row in await conn.fetch(_LOADED, source_uris)}


def _split_new(
    items: Iterable[ParsedItem], loaded: set[str]
) -> tuple[list[ParsedItem], list[ParsedItem]]:
    """Split parsed items into those whose source URI is not loaded yet, and the duplicates."""
    new: list[ParsedItem] = []
    duplicates: list[ParsedItem] = []
    for item in items:
        (duplicates if item.source_uri in loaded else new).append(item)
    return new, duplicates


def _first_of_each(
    records: Iterable[PreparedRecord],
) -> tuple[list[PreparedRecord], list[PreparedRecord]]:
    """Split a batch's records into the first with each source URI, and the repeats.

    This runs after the records were built, so an item that was rejected does not make a later,
    good item with the same URI a duplicate of something that was never loaded.
    """
    keep: list[PreparedRecord] = []
    repeats: list[PreparedRecord] = []
    seen: set[str] = set()
    for record in records:
        if record.source_uri in seen:
            repeats.append(record)
        else:
            seen.add(record.source_uri)
            keep.append(record)
    return keep, repeats


async def _copy_without_duplicates(
    worker: Worker, records: list[PreparedRecord]
) -> list[PreparedRecord]:
    """Write the records that are not loaded yet; upsert their labels; return the duplicates."""
    loaded = await _loaded(worker.conn, [r.source_uri for r in records])
    keep = [r for r in records if r.source_uri not in loaded]
    if keep:
        await _copy(worker, keep, upsert_labels=True)
    return [r for r in records if r.source_uri in loaded]


async def _write(worker: Worker, records: list[PreparedRecord]) -> list[PreparedRecord]:
    """Write a batch; return the records skipped as duplicates."""
    if not records:
        return []
    try:
        await _copy(worker, records, upsert_labels=False)
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


def _duplicate(source: str, source_uri: str) -> Failure:
    return Failure(
        STAGE, source, DUPLICATE_URI, source_uri, ["a record with this URI is already loaded"]
    )


def load_batch(items: list[SourceItem]) -> LoadedBatch:
    """Worker entry point: prepare one batch and ``COPY`` it."""
    worker = current()
    cpu0 = time.process_time()
    parsed, failures = parse_batch(items)
    parse_cpu_s = time.process_time() - cpu0
    loaded: set[str] = worker.run(_loaded(worker.conn, [p.source_uri for p in parsed]))
    new, known = _split_new(parsed, loaded)
    failures.extend(_duplicate(p.source, p.source_uri) for p in known)
    prepared = build_batch(new, worker.base_uri, worker.context_version)
    failures.extend(prepared.failures)
    records, repeats = _first_of_each(prepared.records)
    started = time.perf_counter()
    with worker.write_slots:
        duplicates: list[PreparedRecord] = worker.run(_write(worker, records))
    copy_s = time.perf_counter() - started
    failures.extend(_duplicate(r.source, r.source_uri) for r in (*repeats, *duplicates))
    skipped = {id(r) for r in duplicates}
    written = [r for r in records if id(r) not in skipped]
    findings: dict[str, int] = {}
    for record in written:
        for code, _ in record.findings:
            findings[code] = findings.get(code, 0) + 1
    return LoadedBatch(
        items=len(items),
        written=len(written),
        reminted=sum(1 for r in written if r.alias is not None),
        flagged=sum(1 for r in written if r.findings),
        findings=findings,
        failures=failures,
        repaired_ids=sum(p.repaired_ids for p in new),
        prepare_cpu_s=parse_cpu_s + prepared.cpu_s,
        copy_s=copy_s,
    )


def _batches(items: Iterator[SourceItem], size: int) -> Iterator[list[SourceItem]]:
    while chunk := list(islice(items, size)):
        yield chunk


class _Totals:
    def __init__(self) -> None:
        self.items = 0
        self.written = 0
        self.reminted = 0
        self.flagged = 0
        self.findings: dict[str, int] = {}
        self.repaired_ids = 0
        self.prepare_cpu_s = 0.0
        self.copy_s = 0.0

    def add(self, batch: LoadedBatch) -> None:
        self.items += batch.items
        self.written += batch.written
        self.reminted += batch.reminted
        self.flagged += batch.flagged
        for code, count in batch.findings.items():
            self.findings[code] = self.findings.get(code, 0) + count
        self.repaired_ids += batch.repaired_ids
        self.prepare_cpu_s += batch.prepare_cpu_s
        self.copy_s += batch.copy_s


async def _rewrite_since(conn: asyncpg.Connection) -> datetime:
    """Return the time pass 2 starts from, and record it until the load has finished.

    An earlier load that did not finish left its own, earlier time: keep it, so that this load's
    second pass completes that one's rows as well.
    """
    pending = await get_sync_state(conn, BOOTSTRAP_REWRITE_SINCE)
    if isinstance(pending, str):
        return datetime.fromisoformat(pending)
    since: datetime = await conn.fetchval("SELECT now()")
    await set_sync_state(conn, {BOOTSTRAP_REWRITE_SINCE: since.isoformat()})
    return since


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
    """Load every record of ``input_path`` (a directory or a JSON Lines file); return a summary.

    Raises ``BootstrapError`` if another load is running on the database.
    """
    workers = workers or os.cpu_count() or 1
    dsn = asyncpg_dsn(settings.database_url)
    conn = await asyncpg.connect(dsn)
    try:
        await acquire_load_lock(conn)
        draft_epoch = await has_column(conn, "records", DRAFT_EPOCH_COLUMN)
        since = await _rewrite_since(conn)
        run_id = new_uuid7()
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
                initializer=init_worker,
                initargs=(
                    dsn,
                    settings.base_uri,
                    settings.context_version,
                    draft_epoch,
                    run_id,
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

            # Pass 2 looks every reference up in these tables: give the planner their sizes.
            for table in ("records", "record_aliases", "entity_labels"):
                await conn.execute(f"ANALYZE {table}")
            rewrite = await rewrite_pass(
                conn,
                pool,
                since=since,
                batch=batch,
                inflight=workers + 1,
                progress_every=progress_every,
            )
        versions = {
            BOOTSTRAP_CONTEXT_VERSION: settings.context_version,
            BOOTSTRAP_EXPANDER_VERSION: EXPANDER_VERSION,
        }
        await set_sync_state(conn, versions)
        metatypes_harvested = await harvest_all_metatypes(conn)
        finish_started = time.perf_counter()
        await finish(conn, changed=rewrite["changed"] > 0)
        finish_s = time.perf_counter() - finish_started
        records_total = await conn.fetchval("SELECT count(*) FROM records")
        stale = await index_is_stale(conn, changed=totals.written > 0 or rewrite["changed"] > 0)
    finally:
        await conn.close()
    seconds = time.perf_counter() - started
    return {
        "stage": STAGE,
        "input": str(input_path),
        "run_id": str(run_id),
        "workers": workers,
        "streams": streams,
        "batch": batch,
        "items": totals.items,
        "records_written": totals.written,
        "records_reminted": totals.reminted,
        "records_flagged": totals.flagged,
        "findings": dict(sorted(totals.findings.items())),
        "records_skipped": sum(failures.values()),
        "failures": failures,
        "report": str(report_path),
        "repaired_ids": totals.repaired_ids,
        "seconds": round(seconds, 2),
        "load_seconds": round(load_s, 2),
        "records_per_s": round(totals.written / max(load_s, 1e-9)),
        "prepare_cpu_s": round(totals.prepare_cpu_s, 1),
        "copy_s": round(totals.copy_s, 1),
        "rewrite": rewrite,
        "finish_seconds": round(finish_s, 2),
        "records_total": records_total,
        "metatypes_harvested": metatypes_harvested,
        "index_rebuild_needed": stale,
        "sync_state": versions,
    }
