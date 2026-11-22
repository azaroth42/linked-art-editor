"""Expansion stage: every ``records`` row -> sanitized N-Triples, in a process pool, one ``.nt``.

The main process streams ``data::text`` with a server-side cursor in batches and hands each batch
to a ``ProcessPoolExecutor`` worker (``ntriples.expand_batch``: pyld + pinned context + sanitation);
it writes the returned bytes to one file in completion order. At most ``--inflight`` batches are
outstanding, so memory stays flat. Database: ``LAE_DATABASE_URL``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import asyncpg

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s6_bootstrap.common import cpu_seconds, pg_dsn, write_summary  # noqa: E402
from s6_bootstrap.ntriples import (  # noqa: E402
    enable_context_c14n_memo,
    enable_static_context,
    expand_batch,
)

TIMING_KEYS = ("cpu_s", "parse_s", "to_rdf_s", "serialize_s")
SPLIT_KEYS = ("parse_s", "to_rdf_s", "serialize_s")


def _warm(_: int) -> int:
    """Expand one tiny record so the worker has imported pyld and resolved the context."""
    data, _stats = expand_batch(
        ['{"@context":"https://linked.art/ns/v1/linked-art.json","id":"urn:x:1","type":"Type"}']
    )
    return len(data)


def _init_worker(static_context: bool, memo_c14n: bool) -> None:
    if static_context:
        enable_static_context()
    if memo_c14n:
        enable_context_c14n_memo()


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Run the expansion stage and return its summary."""
    conn = await asyncpg.connect(pg_dsn())
    loop = asyncio.get_running_loop()
    counters: Counter[str] = Counter()
    timings: dict[str, float] = dict.fromkeys(TIMING_KEYS, 0.0)
    out_bytes = 0
    fetch_s = 0.0
    sql = "SELECT data::text FROM records WHERE status = 'active'"
    if args.limit:  # same rows every time, for scaling comparisons
        sql += f" ORDER BY id LIMIT {int(args.limit)}"
    try:
        backend_pid = await conn.fetchval("SELECT pg_backend_pid()")
        backend_cpu0 = cpu_seconds(backend_pid)
        init_args = (args.static_context, args.memo_c14n)
        with (
            ProcessPoolExecutor(args.workers, initializer=_init_worker, initargs=init_args) as pool,
            args.out.open("wb") as out,
        ):
            warm_started = time.perf_counter()
            list(pool.map(_warm, range(args.workers)))
            warm_s = time.perf_counter() - warm_started

            def handle(result: tuple[bytes, dict[str, float]]) -> None:
                nonlocal out_bytes
                data, stats = result
                out.write(data)
                out_bytes += len(data)
                for key, value in stats.items():
                    if key in timings:
                        timings[key] += value
                    else:
                        counters[key] += int(value)

            cpu0 = time.process_time()
            started = time.perf_counter()
            pending: set[asyncio.Future[tuple[bytes, dict[str, float]]]] = set()
            async with conn.transaction():
                cursor = await conn.cursor(sql)
                while True:
                    t0 = time.perf_counter()
                    rows = await cursor.fetch(args.batch)
                    fetch_s += time.perf_counter() - t0
                    if not rows:
                        break
                    texts = [row[0] for row in rows]
                    pending.add(loop.run_in_executor(pool, expand_batch, texts))
                    if len(pending) >= args.inflight:
                        done, pending = await asyncio.wait(
                            pending, return_when=asyncio.FIRST_COMPLETED
                        )
                        for future in done:
                            handle(future.result())
            for future in asyncio.as_completed(pending):
                handle(await future)
            seconds = time.perf_counter() - started
            main_cpu_s = time.process_time() - cpu0
        backend_cpu_s = cpu_seconds(backend_pid) - backend_cpu0
    finally:
        await conn.close()
    records = counters["records"]
    triples = counters["triples"]
    worker_cpu = timings["cpu_s"]
    return {
        "stage": "expand",
        "out": str(args.out),
        "workers": args.workers,
        "static_context": args.static_context,
        "memo_c14n": args.memo_c14n,
        "batch": args.batch,
        "records": records,
        "triples": triples,
        "nt_bytes": out_bytes,
        "seconds": round(seconds, 2),
        "worker_warmup_s": round(warm_s, 2),
        "records_per_s": round(records / seconds),
        "triples_per_s": round(triples / seconds),
        "nt_mb_per_s": round(out_bytes / seconds / 1e6, 1),
        "triples_per_record": round(triples / max(1, records), 1),
        "nt_bytes_per_triple": round(out_bytes / max(1, triples), 1),
        "worker_cpu_s": round(worker_cpu, 1),
        "worker_utilisation": round(worker_cpu / (seconds * args.workers), 3),
        "worker_time_split": {
            key: round(timings[key] / max(sum(timings[k] for k in SPLIT_KEYS), 1e-9), 3)
            for key in SPLIT_KEYS
        },
        "per_worker_records_per_s": round(records / max(worker_cpu, 1e-9)),
        "main_cpu_s": round(main_cpu_s, 1),
        "main_fetch_s": round(fetch_s, 1),
        "backend_cpu_s": round(backend_cpu_s, 1),
        "sanitation": {
            key: value
            for key, value in sorted(counters.items())
            if key not in ("records", "triples")
        },
    }


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the expansion stage, print the summary."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--inflight", type=int, help="outstanding batches (default 3 x workers)")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--static-context", action="store_true", help="tuning: ntriples.enable_static_context"
    )
    parser.add_argument(
        "--memo-c14n", action="store_true", help="tuning: ntriples.enable_context_c14n_memo"
    )
    parser.add_argument("--stats-out", type=Path)
    args = parser.parse_args(argv)
    if args.memo_c14n and not args.static_context:
        parser.error("--memo-c14n needs --static-context")
    if args.inflight is None:
        args.inflight = 3 * args.workers
    write_summary(asyncio.run(run(args)), args.stats_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
