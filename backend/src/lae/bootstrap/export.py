"""``lae bootstrap export-nt``: every active record as RDF, plus sidecar labels, in one file.

The main process reads ``records`` through a server-side cursor inside one ``REPEATABLE READ``
snapshot and hands batches to a process pool; each worker repairs ids (``clean_ids``) and expands
every record with ``lae.rdf.expand_to_ntriples``, the production expansion with all of its
ownership and sanitation checks, using the record's own ``records.context_version``. The main
process appends what comes back to the output file, in completion order; each record's lines
are sorted. A record that raises ``ExpansionError``, or has an id that cannot be repaired, is
written to the report and skipped. With an output name ending in ``.gz``, each worker
gzip-compresses its batch, and the members concatenate into one valid gzip file, so compression
runs in parallel too.

**Sidecar labels.** For every ``entity_labels`` row whose ``source`` is not ``local``, the file
gets ``<uri> rdf:type <class>`` and one ``<uri> rdfs:label "label"@lang`` per language, as quads in
graph ``urn:graph:sidecar:<source>``. The class IRI is what the pinned context maps the row's
``type`` to (``Type`` → ``crm:E55_Type``), the same IRI a local record of that class has. Because
of these quads the file is N-Quads, of which N-Triples is a subset: record triples are in the
default graph, and ``deploy/Qleverfile`` reads the file as ``nq``.

**Watermark.** The snapshot also gives the highest ``outbox_events.id``. Every event up to it is
reflected in the export; ``lae bootstrap build-index`` stores it as ``qlever_index_watermark`` so
that the sync worker replays only later events. It is written, with the counts, to
``<out>.meta.json``. An event whose transaction is still open when the snapshot is taken, with an
id below the maximum, would be in neither; a bootstrap runs before editing starts, and blue/green
rebuilds (Phase 2) handle that case.
"""

from __future__ import annotations

import asyncio
import functools
import gzip
import json
import multiprocessing
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import IO, Any, Final, NamedTuple

import asyncpg
import orjson

from lae.bootstrap.common import (
    EXPANSION_FAILED,
    INVALID_ID,
    NO_RECORD_URI,
    SIDECAR_LABEL_INVALID,
    Failure,
    Report,
    asyncpg_dsn,
)
from lae.bootstrap.sanitize import clean_ids
from lae.labels.upsert import LOCAL_SOURCE
from lae.rdf import EXPANDER_VERSION, ExpansionError, expand_to_ntriples
from lae.rdf.canonical import escape_literal
from lae.rdf.expand import rdf_triples
from lae.rdf.sanitize import clean_iri
from lae.settings import Settings

STAGE: Final = "export"

RDF_TYPE: Final = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL: Final = "http://www.w3.org/2000/01/rdf-schema#label"
SIDECAR_GRAPH_PREFIX: Final = "urn:graph:sidecar:"
"""Graph of a sidecar dataset: ``urn:graph:sidecar:<entity_labels.source>`` (decision A17)."""

GZIP_LEVEL: Final = 6
DEFAULT_BATCH: Final = 500
"""Records per worker task (Spike S6)."""

_PROBE_ID: Final = "urn:lae:class-probe"
_LANGUAGE_TAG: Final = re.compile(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*")

# No ORDER BY: the output is in completion order anyway, and a sequential scan is the cheapest
# way through a large table.
RECORDS_SQL: Final = "SELECT uri, data::text, context_version FROM records WHERE status = 'active'"
SIDECAR_SQL: Final = (
    "SELECT uri, type, labels::text, default_label, source FROM entity_labels WHERE source <> $1"
)


def meta_path(out: Path) -> Path:
    """Where ``export-nt`` writes the export's metadata: ``<out>.meta.json``."""
    return out.with_name(out.name + ".meta.json")


def default_report_path(out: Path) -> Path:
    """The default report of ``export-nt``: ``<out>.report.jsonl``."""
    return out.with_name(out.name + ".report.jsonl")


class ExpandedBatch(NamedTuple):
    """What a worker returns for one batch of records."""

    data: bytes
    """N-Triples lines of the records that expanded, gzip-compressed if requested."""
    records: int
    triples: int
    repaired_ids: int
    failures: list[Failure]
    cpu_s: float


def expand_record(uri: str, text: str, context_version: int) -> tuple[list[str], int]:
    """Return the sorted N-Triples lines of one stored record and the number of repaired ids.

    Raises ``ExpansionError`` if it cannot be expanded and ``ValueError`` listing the ids that
    cannot be repaired.
    """
    cleaned = clean_ids(orjson.loads(text))
    if cleaned.invalid:
        raise ValueError(cleaned.invalid)
    lines = expand_to_ntriples(cleaned.document, context_version, record_uri=uri)
    return sorted(lines), len(cleaned.repaired)


def expand_batch(rows: list[tuple[str, str, int]], compress: bool) -> ExpandedBatch:
    """Worker entry point: expand ``(uri, data, context_version)`` rows into N-Triples bytes."""
    cpu0 = time.process_time()
    chunks: list[str] = []
    failures: list[Failure] = []
    records = triples = repaired = 0
    for uri, text, context_version in rows:
        try:
            lines, repaired_ids = expand_record(uri, text, context_version)
        except ExpansionError as error:
            failures.append(
                Failure(STAGE, uri, EXPANSION_FAILED, uri, [f"{type(error).__name__}: {error}"])
            )
            continue
        except ValueError as error:
            invalid = [str(item) for item in error.args[0]]
            code = NO_RECORD_URI if uri in invalid else INVALID_ID
            failures.append(Failure(STAGE, uri, code, uri, invalid))
            continue
        chunks.append("".join(f"{line}\n" for line in lines))
        records += 1
        triples += len(lines)
        repaired += repaired_ids
    data = "".join(chunks).encode("utf-8")
    if compress and data:
        data = gzip.compress(data, compresslevel=GZIP_LEVEL, mtime=0)
    return ExpandedBatch(data, records, triples, repaired, failures, time.process_time() - cpu0)


@functools.cache
def class_iri(type_name: str, context_version: int) -> str | None:
    """Return the class IRI the pinned context gives ``type_name``, or ``None`` if it has none."""
    try:
        triples = rdf_triples({"id": _PROBE_ID, "type": type_name}, context_version)
    except ExpansionError:
        return None
    for triple in triples:
        if triple["predicate"]["value"] == RDF_TYPE and triple["object"]["type"] == "IRI":
            value: str = triple["object"]["value"]
            return value
    return None


def sidecar_graph(source: str) -> str:
    """Return the graph IRI of a sidecar dataset."""
    return SIDECAR_GRAPH_PREFIX + source


def _writable(iri: str) -> bool:
    return clean_iri(iri) == iri


def _label_literal(label: str, language: str | None) -> str:
    text = f'"{escape_literal(label)}"'
    if language is not None and _LANGUAGE_TAG.fullmatch(language):
        return f"{text}@{language.lower()}"
    return text


def sidecar_label_quads(
    uri: str,
    type_name: str,
    labels: dict[str, str],
    default_label: str,
    source: str,
    context_version: int,
) -> list[str]:
    """Return the N-Quads lines for one sidecar ``entity_labels`` row.

    One ``rdf:type`` line, then one ``rdfs:label`` line per language in ``labels`` (a language
    key that is not a BCP 47 tag gives a plain literal), or the ``default_label`` as a plain
    literal if ``labels`` is empty. Raises ``ValueError`` if the URI or the graph IRI cannot be
    written, or the pinned context does not define ``type_name``.
    """
    graph = sidecar_graph(source)
    if not _writable(uri):
        raise ValueError(f"not a valid absolute IRI: {uri!r}")
    if not _writable(graph):
        raise ValueError(f"source {source!r} does not give a valid graph IRI")
    klass = class_iri(type_name, context_version)
    if klass is None:
        raise ValueError(f"type {type_name!r} is not a class of the pinned context")
    lines = [f"<{uri}> <{RDF_TYPE}> <{klass}> <{graph}> ."]
    items: list[tuple[str | None, str]] = list(labels.items()) or [(None, default_label)]
    for language, label in items:
        if isinstance(label, str) and label:
            lines.append(f"<{uri}> <{RDFS_LABEL}> {_label_literal(label, language)} <{graph}> .")
    return lines


class _Writer:
    """Appends export chunks to the output file and counts what went in."""

    def __init__(self, out: IO[bytes], report: Report) -> None:
        self.out = out
        self.report = report
        self.bytes = 0
        self.records = 0
        self.triples = 0
        self.repaired_ids = 0
        self.worker_cpu_s = 0.0

    def add(self, batch: ExpandedBatch) -> None:
        self.out.write(batch.data)
        self.bytes += len(batch.data)
        self.records += batch.records
        self.triples += batch.triples
        self.repaired_ids += batch.repaired_ids
        self.worker_cpu_s += batch.cpu_s
        self.report.write(batch.failures)


async def _export_records(
    conn: asyncpg.Connection,
    pool: ProcessPoolExecutor,
    writer: _Writer,
    *,
    compress: bool,
    batch: int,
    inflight: int,
    progress_every: int,
) -> None:
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Future[ExpandedBatch]] = set()
    started = time.perf_counter()
    next_progress = progress_every

    def collect(done: set[asyncio.Future[ExpandedBatch]]) -> None:
        nonlocal next_progress
        for future in done:
            writer.add(future.result())
        if progress_every and writer.records >= next_progress:
            rate = writer.records / max(time.perf_counter() - started, 1e-9)
            print(f"export-nt: {writer.records} records, {rate:.0f}/s", file=sys.stderr)
            next_progress += progress_every

    cursor = await conn.cursor(RECORDS_SQL)
    while rows := await cursor.fetch(batch):
        texts = [(row[0], row[1], row[2]) for row in rows]
        pending.add(loop.run_in_executor(pool, expand_batch, texts, compress))
        if len(pending) >= inflight:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            collect(done)
    if pending:
        done, _ = await asyncio.wait(pending)
        collect(done)


async def _export_sidecar_labels(
    conn: asyncpg.Connection,
    out: IO[bytes],
    report: Report,
    *,
    compress: bool,
    context_version: int,
) -> tuple[int, int, int]:
    """Write the sidecar label quads; return ``(rows, quads, bytes)``."""
    rows = quads = written = 0
    cursor = await conn.cursor(SIDECAR_SQL, LOCAL_SOURCE)
    while records := await cursor.fetch(10_000):
        lines: list[str] = []
        failures: list[Failure] = []
        for uri, type_name, labels_text, default_label, source in records:
            labels = json.loads(labels_text)
            try:
                row_lines = sidecar_label_quads(
                    uri,
                    type_name,
                    labels if isinstance(labels, dict) else {},
                    default_label,
                    source,
                    context_version,
                )
            except ValueError as error:
                failures.append(Failure(STAGE, uri, SIDECAR_LABEL_INVALID, uri, [str(error)]))
                continue
            lines.extend(row_lines)
            rows += 1
        data = "".join(f"{line}\n" for line in lines).encode("utf-8")
        if compress and data:
            data = gzip.compress(data, compresslevel=GZIP_LEVEL, mtime=0)
        out.write(data)
        written += len(data)
        quads += len(lines)
        report.write(failures)
    return rows, quads, written


async def export_nt(
    out: Path,
    *,
    settings: Settings,
    workers: int | None = None,
    batch: int = DEFAULT_BATCH,
    inflight: int | None = None,
    report_path: Path | None = None,
    progress_every: int = 100_000,
) -> dict[str, Any]:
    """Run the export and return its summary (also written to ``<out>.meta.json``)."""
    workers = workers or os.cpu_count() or 1
    inflight = inflight or 3 * workers
    report_path = report_path or default_report_path(out)
    compress = out.name.endswith(".gz")
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    conn = await asyncpg.connect(asyncpg_dsn(settings.database_url))
    try:
        with (
            Report(report_path) as report,
            out.open("wb") as handle,
            ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) as pool,
        ):
            writer = _Writer(handle, report)
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                watermark = int(
                    await conn.fetchval("SELECT coalesce(max(id), 0) FROM outbox_events")
                )
                context_versions = sorted(
                    row[0]
                    for row in await conn.fetch(
                        "SELECT DISTINCT context_version FROM records WHERE status = 'active'"
                    )
                )
                await _export_records(
                    conn,
                    pool,
                    writer,
                    compress=compress,
                    batch=batch,
                    inflight=inflight,
                    progress_every=progress_every,
                )
                records_s = time.perf_counter() - started
                sidecar_rows, sidecar_quads, sidecar_bytes = await _export_sidecar_labels(
                    conn,
                    handle,
                    report,
                    compress=compress,
                    context_version=settings.context_version,
                )
            failures = dict(sorted(report.counts.items()))
    finally:
        await conn.close()
    seconds = time.perf_counter() - started
    summary: dict[str, Any] = {
        "stage": STAGE,
        "out": str(out),
        "format": "nq",
        "gzip": compress,
        "watermark": watermark,
        "context_version": settings.context_version,
        "record_context_versions": context_versions,
        "expander_version": EXPANDER_VERSION,
        "workers": workers,
        "records": writer.records,
        "triples": writer.triples,
        "records_failed": sum(
            count for code, count in failures.items() if code != SIDECAR_LABEL_INVALID
        ),
        "repaired_ids": writer.repaired_ids,
        "sidecar_rows": sidecar_rows,
        "sidecar_quads": sidecar_quads,
        "sidecar_rows_failed": failures.get(SIDECAR_LABEL_INVALID, 0),
        "failures": failures,
        "report": str(report_path),
        "bytes": writer.bytes + sidecar_bytes,
        "seconds": round(seconds, 2),
        "records_seconds": round(records_s, 2),
        "records_per_s": round(writer.records / max(records_s, 1e-9)),
        "triples_per_s": round(writer.triples / max(records_s, 1e-9)),
        "worker_cpu_s": round(writer.worker_cpu_s, 1),
        "worker_utilisation": round(writer.worker_cpu_s / max(records_s * workers, 1e-9), 3),
    }
    meta_path(out).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary
