"""COPY stage: stream JSON Lines records into ``records`` with binary ``COPY`` in batches.

Each line becomes one row: ``id`` from the URI's UUID, ``version=1``, ``status='active'``,
``context_version=1``, ``etag`` = BLAKE2b-128 of the canonical JSON line, ``data`` = the line
(``jsonb``). ``--streams N`` runs N processes, each with its own connection, taking every N-th
batch. Database: ``LAE_DATABASE_URL`` (the slot database).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Final

import asyncpg
import orjson

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s6_bootstrap.common import cpu_seconds, pg_dsn, write_summary  # noqa: E402

COLUMNS: Final = ("id", "uri", "type", "version", "status", "etag", "data", "context_version")
CONTEXT_VERSION: Final = 1
PG_SETTINGS: Final = (
    "server_version",
    "shared_buffers",
    "max_wal_size",
    "checkpoint_timeout",
    "wal_level",
    "wal_compression",
    "synchronous_commit",
    "full_page_writes",
    "default_toast_compression",
)


def to_rows(lines: list[bytes]) -> list[tuple[Any, ...]]:
    """Build ``records`` rows (in ``COLUMNS`` order) from canonical JSON lines."""
    rows = []
    for line in lines:
        record = orjson.loads(line)
        uri: str = record["id"]
        etag = hashlib.blake2b(line, digest_size=16).hexdigest()
        rows.append(
            (
                uuid.UUID(uri[-36:]),
                uri,
                record["type"],
                1,
                "active",
                etag,
                line.decode(),
                CONTEXT_VERSION,
            )
        )
    return rows


async def copy_stream(
    dsn: str, path: Path, stream: int, streams: int, batch_size: int, limit: int | None
) -> dict[str, Any]:
    """Copy every ``streams``-th batch (offset ``stream``) of ``path`` into ``records``."""
    conn = await asyncpg.connect(dsn)
    try:
        backend_pid = await conn.fetchval("SELECT pg_backend_pid()")
        backend_cpu0 = cpu_seconds(backend_pid)
        started = time.perf_counter()
        prep_s = copy_s = 0.0
        rows_done = bytes_done = 0
        progress: list[tuple[int, float]] = []
        batch: list[bytes] = []

        async def flush() -> None:
            nonlocal prep_s, copy_s, rows_done, bytes_done
            t0 = time.perf_counter()
            rows = to_rows(batch)
            t1 = time.perf_counter()
            await conn.copy_records_to_table("records", records=rows, columns=COLUMNS)
            t2 = time.perf_counter()
            prep_s += t1 - t0
            copy_s += t2 - t1
            rows_done += len(batch)
            bytes_done += sum(len(line) for line in batch)
            progress.append((rows_done, t2 - started))
            batch.clear()

        with path.open("rb") as source:
            for line_no, line in enumerate(source):
                if limit is not None and line_no >= limit:
                    break
                if (line_no // batch_size) % streams != stream:
                    continue
                batch.append(line.rstrip(b"\n"))
                if len(batch) == batch_size:
                    await flush()
            if batch:
                await flush()
        seconds = time.perf_counter() - started
        return {
            "stream": stream,
            "rows": rows_done,
            "bytes": bytes_done,
            "seconds": seconds,
            "client_prep_s": prep_s,
            "copy_s": copy_s,
            "backend_cpu_s": cpu_seconds(backend_pid) - backend_cpu0,
            "progress": progress,
        }
    finally:
        await conn.close()


def _run_stream(*args: Any) -> dict[str, Any]:
    return asyncio.run(copy_stream(*args))


def _window_rates(progress: list[tuple[int, float]], window: int) -> list[int]:
    """Records/s for each consecutive ``window`` rows (shows whether COPY slows as it grows)."""
    rates, last_rows, last_t, edge = [], 0, 0.0, window
    for rows, t in progress:
        if rows >= edge:
            rates.append(round((rows - last_rows) / (t - last_t)))
            last_rows, last_t, edge = rows, t, rows + window
    return rates


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Run the COPY stage and return its summary."""
    dsn = pg_dsn()
    conn = await asyncpg.connect(dsn)
    try:
        if args.truncate:
            await conn.execute("TRUNCATE records CASCADE")
            await conn.execute("CHECKPOINT")
        settings = {
            name: await conn.fetchval(f"SELECT current_setting('{name}')") for name in PG_SETTINGS
        }
        wal0 = await conn.fetchval("SELECT pg_current_wal_lsn()")
        started = time.perf_counter()
        stream_args = [
            (dsn, args.input, k, args.streams, args.batch, args.limit) for k in range(args.streams)
        ]
        if args.streams == 1:
            streams = [await copy_stream(*stream_args[0])]
        else:
            with ProcessPoolExecutor(args.streams) as pool:
                streams = list(pool.map(_run_stream, *zip(*stream_args, strict=True)))
        seconds = time.perf_counter() - started
        wal_bytes = await conn.fetchval(
            "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), $1::pg_lsn)", wal0
        )
        await conn.execute("ANALYZE records")
        sizes = await conn.fetchrow(
            """SELECT count(*) AS rows,
                      pg_table_size('records') AS table_bytes,
                      pg_relation_size('records') AS heap_bytes,
                      pg_indexes_size('records') AS index_bytes,
                      pg_total_relation_size('records') AS total_bytes
               FROM records"""
        )
    finally:
        await conn.close()
    rows = sum(s["rows"] for s in streams)
    input_bytes = sum(s["bytes"] for s in streams)
    return {
        "stage": "copy",
        "input": str(args.input),
        "streams": args.streams,
        "batch": args.batch,
        "rows": rows,
        "seconds": round(seconds, 2),
        "records_per_s": round(rows / seconds),
        "input_mb_per_s": round(input_bytes / seconds / 1e6, 1),
        "client_prep_s": round(sum(s["client_prep_s"] for s in streams), 2),
        "copy_s": round(sum(s["copy_s"] for s in streams), 2),
        "backend_cpu_s": round(sum(s["backend_cpu_s"] for s in streams), 2),
        "wal_bytes": int(wal_bytes),
        "rates_per_100k": _window_rates(streams[0]["progress"], 100_000 // args.streams),
        "table": dict(sizes),
        "postgres": settings,
    }


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the COPY stage, print the summary."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--batch", type=int, default=10_000)
    parser.add_argument("--streams", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--truncate", action="store_true", help="TRUNCATE records first")
    parser.add_argument("--stats-out", type=Path)
    args = parser.parse_args(argv)
    write_summary(asyncio.run(run(args)), args.stats_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
