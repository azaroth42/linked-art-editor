"""The second pass of a bulk load: rewrite references to re-minted records, strip known labels.

The first pass (``lae.bootstrap.load``) writes every record under its own new URI but leaves its
references as the source gave them, because a worker sees one batch and the target of a
reference may be anywhere in the input. Once every record and alias of the load is in
PostgreSQL, this pass goes over the loaded rows again, in batches, in the same worker pool:

1. **Rewrite.** Every ``id`` in the batch that is a ``record_aliases.alias_uri`` is replaced by
   the URI of the alias's record (``lae.bootstrap.remint.rewrite_references``). The lookup is
   one indexed query per batch, so the alias map never has to fit in memory. Aliases of earlier
   loads count too: that is how a later load's references to an earlier one resolve.
2. **Normalize.** ``_label`` is removed from every reference whose target has an
   ``entity_labels`` row (``lae.labels.normalize_refs``; spec §3.2, ADR-0006, ADR-0019), as
   publish does. A reference to a target with no row keeps its label. Load AAT labels
   (``lae bootstrap aat-labels``) first, or run ``lae bootstrap rewrite-refs`` afterwards, for
   concept references to be normalized as well.
3. **Update** the rows that changed: ``data``, ``etag`` (``record_etag``), and the label row's
   ``primary_classification`` when the first ``classified_as`` was itself re-minted.

**Which rows.** Only rows still exactly as a bulk load wrote them: ``version = 1``, active, no
``record_revisions`` row and no open draft. A record that has been published through the editor
is never changed here: its history and QLever already hold the published form. The update
repeats these conditions and compares the ``etag`` read, so a row that was changed, or for which
a draft was opened, after the batch was selected is left alone.

**Scope and recovery.** ``lae bootstrap load`` runs the pass over the rows created since the
load began. While a load is unfinished ``sync_state.bootstrap_rewrite_since`` holds that time;
if a load is interrupted, the next load starts its pass from there, so the earlier rows are
finished too. Until then they are consistent but not final: their references still carry
source URIs, each of which is an alias that resolves. ``lae bootstrap rewrite-refs`` runs the
pass over *every* bulk-loaded row; use it after an interruption, after loading labels, or to
point old rows at records a later load brought in. The pass is idempotent.

**Order of the stages.** The pass changes stored JSON-LD without an outbox event, like the load
itself, so it belongs before ``export-nt`` and ``build-index``. After any ``load`` or
``rewrite-refs`` on a database that already has an index, export and build the index again;
both commands say so (``index_rebuild_needed`` in the summary, and a line on stderr) when they
changed anything and ``sync_state`` has an index watermark.

**Why this cannot make a record unexpandable.** The first pass rejected every record that
``expand_to_ntriples`` refuses. This pass only (a) replaces, below the root, one valid absolute
IRI that is not the record's by another that is not the record's (never its own URI, never one
of its node IRIs), so each object stays foreign and stays what ADR-0021 classified it as (the
classification looks at an object's keys and ``type``, not at its ``id``), and (b) removes
``_label``, a key the classification ignores. Neither can introduce a blank node, a subject the
record does not own, an invalid IRI or an invalid literal. ``export-nt`` still checks every
record and reports any it cannot expand.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from typing import Any, Final, NamedTuple
from uuid import UUID

import asyncpg
import orjson

from lae.api.records import record_etag
from lae.bootstrap.common import (
    BOOTSTRAP_REWRITE_SINCE,
    LOAD_LOCK,
    QLEVER_INDEX_WATERMARK,
    BootstrapError,
    asyncpg_dsn,
    get_sync_state,
)
from lae.bootstrap.remint import reference_targets, rewrite_references
from lae.bootstrap.worker import current, init_worker
from lae.ids import new_uuid7
from lae.labels.normalize import LABEL_KEY, normalize_refs
from lae.labels.refs import iter_references
from lae.labels.upsert import primary_classification
from lae.projection import canonical_json
from lae.settings import Settings
from lae.vocab.metatypes import harvest_all_metatypes

STAGE: Final = "rewrite"
DEFAULT_BATCH: Final = 10_000
DEFAULT_STREAMS: Final = 4

_FIRST_ID: Final = UUID(int=0)

# Keyset pagination over the primary key: no long transaction, and bounded memory.
_BULK_ROWS: Final = """
SELECT r.id FROM records r
WHERE r.id > $1
  AND r.version = 1
  AND r.status = 'active'
  AND ($2::timestamptz IS NULL OR r.created_at >= $2)
  AND NOT EXISTS (SELECT 1 FROM record_revisions v WHERE v.record_id = r.id)
  AND NOT EXISTS (SELECT 1 FROM crdt_drafts d WHERE d.record_id = r.id)
ORDER BY r.id
LIMIT $3
"""
_FETCH: Final = "SELECT id, uri, etag, data::text FROM records WHERE id = ANY($1::uuid[])"
_ALIASES: Final = """
SELECT a.alias_uri, r.uri FROM record_aliases a JOIN records r ON r.id = a.record_id
WHERE a.alias_uri = ANY($1::text[])
"""
_KNOWN: Final = "SELECT uri FROM entity_labels WHERE uri = ANY($1::text[])"
_UPDATE: Final = """
UPDATE records r SET data = $2::jsonb, etag = $3
WHERE r.id = $1 AND r.etag = $4 AND r.version = 1 AND r.status = 'active'
  AND NOT EXISTS (SELECT 1 FROM record_revisions v WHERE v.record_id = r.id)
  AND NOT EXISTS (SELECT 1 FROM crdt_drafts d WHERE d.record_id = r.id)
"""
_UPDATE_CLASSIFICATION: Final = (
    "UPDATE entity_labels SET primary_classification = $2 WHERE uri = $1"
)


class RewrittenBatch(NamedTuple):
    """What a worker returns for one batch of record ids."""

    records: int
    changed: int
    references_rewritten: int
    labels_stripped: int
    cpu_s: float
    write_s: float


def finalize(
    document: dict[str, Any], uri: str, aliases: dict[str, str], known: set[str]
) -> tuple[dict[str, Any], int, int]:
    """Rewrite and normalize one stored document.

    Returns ``(document, references rewritten, labels stripped)``; the document is the input
    object itself when nothing changed.
    """
    rewritten, count = rewrite_references(document, uri, aliases) if aliases else (document, 0)
    stripped = sum(
        1
        for reference in iter_references(rewritten)
        if LABEL_KEY in reference and reference["id"] in known
    )
    if stripped:
        rewritten = normalize_refs(rewritten, known)
    return rewritten, count, stripped


async def _rewrite(ids: list[UUID]) -> RewrittenBatch:
    worker = current()
    conn = worker.conn
    cpu0 = time.process_time()
    rows = await conn.fetch(_FETCH, ids)
    documents = [(row[0], row[1], row[2], orjson.loads(row[3])) for row in rows]
    targets: set[str] = set()
    for _, uri, _, document in documents:
        targets |= reference_targets(document, uri)
    aliases: dict[str, str] = {}
    if targets:
        aliases = {row[0]: row[1] for row in await conn.fetch(_ALIASES, list(targets))}
    resolved = (targets - aliases.keys()) | set(aliases.values())
    known: set[str] = set()
    if resolved:
        known = {row[0] for row in await conn.fetch(_KNOWN, list(resolved))}

    updates: list[tuple[UUID, str, str, str]] = []
    classifications: list[tuple[str, str | None]] = []
    references = labels = 0
    for record_id, uri, etag, document in documents:
        final, count, stripped = finalize(document, uri, aliases, known)
        if not count and not stripped:
            continue
        references += count
        labels += stripped
        updates.append((record_id, canonical_json(final).decode("utf-8"), record_etag(final), etag))
        classification = primary_classification(final)
        if classification != primary_classification(document):
            classifications.append((uri, classification))
    cpu_s = time.process_time() - cpu0
    started = time.perf_counter()
    if updates:
        with worker.write_slots:
            async with conn.transaction():
                await conn.executemany(_UPDATE, updates)
                if classifications:
                    await conn.executemany(_UPDATE_CLASSIFICATION, classifications)
    write_s = time.perf_counter() - started
    return RewrittenBatch(len(documents), len(updates), references, labels, cpu_s, write_s)


def rewrite_batch(ids: list[UUID]) -> RewrittenBatch:
    """Worker entry point: finalize the records with these ids."""
    return current().run(_rewrite(ids))  # type: ignore[no-any-return]


async def rewrite_pass(
    conn: asyncpg.Connection,
    pool: ProcessPoolExecutor,
    *,
    since: datetime | None,
    batch: int,
    inflight: int,
    progress_every: int = 100_000,
) -> dict[str, Any]:
    """Run the pass over the bulk rows created at or after ``since`` (all of them if ``None``)."""
    loop = asyncio.get_running_loop()
    started = time.perf_counter()
    totals = {"records": 0, "changed": 0, "references_rewritten": 0, "labels_stripped": 0}
    cpu_s = write_s = 0.0
    next_progress = progress_every
    pending: set[asyncio.Future[RewrittenBatch]] = set()

    def collect(done: set[asyncio.Future[RewrittenBatch]]) -> None:
        nonlocal cpu_s, write_s, next_progress
        for future in done:
            result = future.result()
            totals["records"] += result.records
            totals["changed"] += result.changed
            totals["references_rewritten"] += result.references_rewritten
            totals["labels_stripped"] += result.labels_stripped
            cpu_s += result.cpu_s
            write_s += result.write_s
        if progress_every and totals["records"] >= next_progress:
            rate = totals["records"] / max(time.perf_counter() - started, 1e-9)
            print(f"rewrite: {totals['records']} records, {rate:.0f}/s", file=sys.stderr)
            next_progress += progress_every

    last = _FIRST_ID
    while True:
        ids = [row[0] for row in await conn.fetch(_BULK_ROWS, last, since, batch)]
        if not ids:
            break
        last = ids[-1]
        pending.add(loop.run_in_executor(pool, rewrite_batch, ids))
        if len(pending) >= inflight:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            collect(done)
    if pending:
        done, _ = await asyncio.wait(pending)
        collect(done)
    seconds = time.perf_counter() - started
    return {
        **totals,
        "seconds": round(seconds, 2),
        "records_per_s": round(totals["records"] / max(seconds, 1e-9)),
        "cpu_s": round(cpu_s, 1),
        "write_s": round(write_s, 1),
    }


async def drop_superseded_labels(conn: asyncpg.Connection) -> int:
    """Delete label-only rows whose URI now names a loaded record (owner decision, ADR-0030).

    A sidecar or seed row (``source <> 'local'``, e.g. an AAT label) whose URI became the alias of
    a bulk-loaded record is superseded by that record's own ``entity_labels`` row, so keeping it
    would make autocomplete offer the same entity twice. Runs after the reference rewrite and
    before the metatype harvest, so harvested metatypes land on the record's row. Returns the
    number of rows deleted.
    """
    status: str = await conn.execute(
        """
        DELETE FROM entity_labels AS el
        USING record_aliases AS a
        WHERE el.uri = a.alias_uri AND el.source <> 'local'
        """
    )
    return int(status.split()[-1])


async def acquire_load_lock(conn: asyncpg.Connection) -> None:
    """Take the session advisory lock of bulk loads, or raise ``BootstrapError``."""
    if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOAD_LOCK):
        raise BootstrapError(
            "another lae bootstrap load or rewrite-refs is running on this database"
        )


async def has_column(conn: asyncpg.Connection, table: str, column: str) -> bool:
    """Whether ``table.column`` exists in the current schema."""
    found: bool = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = $1 AND column_name = $2)",
        table,
        column,
    )
    return found


async def index_is_stale(conn: asyncpg.Connection, *, changed: bool) -> bool:
    """Whether a QLever index exists that no longer matches ``records`` (says so on stderr).

    ``load`` and ``rewrite-refs`` write no outbox events, so the sync worker cannot bring an
    index that was built before them up to date: only ``export-nt`` and ``build-index`` can.
    """
    stale = changed and await get_sync_state(conn, QLEVER_INDEX_WATERMARK) is not None
    if stale:
        print(
            "lae bootstrap: records changed after the QLever index was built; "
            "run export-nt and build-index again",
            file=sys.stderr,
        )
    return stale


async def finish(conn: asyncpg.Connection, *, changed: bool) -> None:
    """Clear the unfinished-load marker and refresh statistics (vacuum if rows were updated)."""
    await conn.execute("DELETE FROM sync_state WHERE key = $1", BOOTSTRAP_REWRITE_SINCE)
    # Every updated row left a dead tuple behind; reclaim them before the table is read in bulk.
    await conn.execute("VACUUM (ANALYZE) records" if changed else "ANALYZE records")
    for table in ("entity_labels", "record_aliases", "qa_findings"):
        await conn.execute(f"ANALYZE {table}")


async def rewrite_refs(
    *,
    settings: Settings,
    batch: int = DEFAULT_BATCH,
    streams: int = DEFAULT_STREAMS,
    workers: int | None = None,
) -> dict[str, Any]:
    """``lae bootstrap rewrite-refs``: run the pass over every bulk-loaded row; return a summary."""
    workers = workers or os.cpu_count() or 1
    dsn = asyncpg_dsn(settings.database_url)
    conn = await asyncpg.connect(dsn)
    try:
        await acquire_load_lock(conn)
        pending_since = await get_sync_state(conn, BOOTSTRAP_REWRITE_SINCE)
        draft_epoch = await has_column(conn, "records", "draft_epoch")
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(
            workers,
            mp_context=context,
            initializer=init_worker,
            initargs=(
                dsn,
                settings.base_uri,
                settings.context_version,
                draft_epoch,
                new_uuid7(),
                context.Semaphore(streams),
            ),
        ) as pool:
            summary = await rewrite_pass(conn, pool, since=None, batch=batch, inflight=workers + 1)
        superseded_labels_deleted = await drop_superseded_labels(conn)
        # References now name their targets by their final URIs: harvest again (ADR-0032).
        metatypes_harvested = await harvest_all_metatypes(conn) if summary["changed"] else 0
        await finish(conn, changed=summary["changed"] > 0)
        stale = await index_is_stale(conn, changed=summary["changed"] > 0)
    finally:
        await conn.close()
    return {
        "stage": STAGE,
        "workers": workers,
        "streams": streams,
        "batch": batch,
        "completed_interrupted_load": pending_since is not None,
        **summary,
        "superseded_labels_deleted": superseded_labels_deleted,
        "metatypes_harvested": metatypes_harvested,
        "index_rebuild_needed": stale,
    }
