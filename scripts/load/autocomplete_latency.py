"""Autocomplete latency against the loaded index (Phase 1 Step 1.22): p95 per prefix length.

Sends ``GET /api/search/autocomplete?q=<prefix>&types=<classes>`` to a running ``lae api`` (with
QLever configured) one request at a time and measures the time to the full answer at the client.
The prefixes are drawn deterministically (``--seed``) from the labels in ``entity_labels``, so they
are prefixes that real entities have: a random row of the requested classes, a random word of its
label, the first ``n`` characters of it.

The ``types`` of each request rotate through what the UI profiles ask for (``Person,Group`` for a
creator, ``Place`` for a place, ``Type`` for a broader concept), with the prefix drawn from labels
of those classes.

Four sets of ``--count`` distinct prefixes each:

- ``2``, ``3``: prefixes of exactly two and three characters (from words at least that long);
- ``4+``: four to eight characters (never longer than the word);
- ``mix``: lengths drawn uniformly from 2 to 6, the guide's "200 random prefixes".

Before each set QLever's cache is cleared (``cmd=clear-cache`` with the access token), and no
prefix repeats within a set, so the numbers are not flattered by QLever answering a repeat from
its cache. The first ``--warmup`` requests of each set (other prefixes) are not counted.

    uv run --project backend python scripts/load/autocomplete_latency.py \\
        --api http://127.0.0.1:9035 --qlever http://127.0.0.1:7035 \\
        --qlever-token "$LAE_QLEVER_ACCESS_TOKEN" --database-url "$LAE_DATABASE_URL"

Prints a JSON summary; ``--out`` also keeps every request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import statistics
import sys
import time
from typing import Any, Final

import asyncpg
import httpx

CLASS_GROUPS: Final = (("Person", "Group"), ("Place",), ("Type",))
SETS: Final = {"2": (2, 2), "3": (3, 3), "4+": (4, 8), "mix": (2, 6)}
_WORD: Final = re.compile(r"[^\W_]+")


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q / 100 * len(ordered))) - 1]


async def label_words(conn: asyncpg.Connection, classes: tuple[str, ...], sample: int) -> list[str]:
    """Words of the labels of a deterministic sample of ``entity_labels`` rows of ``classes``."""
    rows = await conn.fetch(
        """
        SELECT default_label FROM entity_labels
        WHERE type = ANY($1::text[]) AND default_label <> ''
        ORDER BY md5(uri) LIMIT $2
        """,
        list(classes),
        sample,
    )
    return [word for row in rows for word in _WORD.findall(row[0])]


async def read_database(dsn: str) -> tuple[dict[tuple[str, ...], list[str]], dict[str, int], int]:
    """Label words per class group, ``entity_labels`` rows per class group, ``records`` rows."""
    conn = await asyncpg.connect(dsn)
    try:
        words = {classes: await label_words(conn, classes, 20_000) for classes in CLASS_GROUPS}
        counts = {
            "+".join(classes): await conn.fetchval(
                "SELECT count(*) FROM entity_labels WHERE type = ANY($1::text[])", list(classes)
            )
            for classes in CLASS_GROUPS
        }
        records = await conn.fetchval("SELECT count(*) FROM records")
    finally:
        await conn.close()
    return words, counts, records


def draw_prefixes(
    words: dict[tuple[str, ...], list[str]], low: int, high: int, count: int, rng: random.Random
) -> list[tuple[str, tuple[str, ...]]]:
    """``count`` distinct ``(prefix, classes)`` pairs with prefix lengths in ``low..high``."""
    chosen: list[tuple[str, tuple[str, ...]]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    attempts = 0
    while len(chosen) < count and attempts < count * 1000:
        attempts += 1
        classes = CLASS_GROUPS[len(chosen) % len(CLASS_GROUPS)]
        word = rng.choice(words[classes])
        length = rng.randint(low, high)
        if len(word) < low:
            continue
        prefix = word[: min(length, len(word))]
        key = (prefix.lower(), classes)
        if key in seen:
            continue
        seen.add(key)
        chosen.append((prefix, classes))
    if len(chosen) < count:
        raise SystemExit(f"only {len(chosen)} distinct prefixes of {low}..{high} characters")
    return chosen


def clear_cache(qlever: httpx.Client, token: str | None) -> None:
    if not token:
        return
    qlever.get("/", params={"cmd": "clear-cache"}, headers={"Authorization": f"Bearer {token}"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", required=True)
    parser.add_argument("--qlever", required=True, help="QLever base URL (cache clearing)")
    parser.add_argument("--qlever-token")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20261123)
    parser.add_argument("--sets", default=",".join(SETS), help="comma-separated: 2,3,4+,mix")
    parser.add_argument(
        "--keep-cache",
        action="store_true",
        help="do not clear QLever's cache (a second run then measures repeated prefixes)",
    )
    parser.add_argument("--label", default="", help="free text stored in the summary")
    parser.add_argument("--out")
    args = parser.parse_args(argv)

    dsn = re.sub(r"^postgresql\+asyncpg://", "postgresql://", args.database_url)
    words, counts, records = asyncio.run(read_database(dsn))

    rng = random.Random(args.seed)
    summary: dict[str, Any] = {
        "label": args.label,
        "records": records,
        "entity_labels_by_class": counts,
        "count_per_set": args.count,
        "seed": args.seed,
        "qlever_cache_cleared": not args.keep_cache,
        "sets": {},
    }
    rows: list[dict[str, Any]] = []
    with (
        httpx.Client(base_url=args.api, timeout=60.0) as api,
        httpx.Client(base_url=args.qlever, timeout=60.0) as qlever,
    ):
        for name in args.sets.split(","):
            low, high = SETS[name]
            prefixes = draw_prefixes(words, low, high, args.count + args.warmup, rng)
            if not args.keep_cache:
                clear_cache(qlever, args.qlever_token)
            times: list[float] = []
            by_class: dict[str, list[float]] = {}
            errors = 0
            empty = 0
            for index, (prefix, classes) in enumerate(prefixes):
                started = time.perf_counter()
                response = api.get(
                    "/api/search/autocomplete", params={"q": prefix, "types": ",".join(classes)}
                )
                elapsed = (time.perf_counter() - started) * 1000
                if index < args.warmup:
                    continue
                ok = response.status_code == 200
                hits = len(response.json()) if ok else None
                errors += not ok
                empty += hits == 0
                rows.append(
                    {"set": name, "q": prefix, "types": classes, "ms": elapsed, "hits": hits}
                )
                if ok:
                    times.append(elapsed)
                    by_class.setdefault("+".join(classes), []).append(elapsed)
            summary["sets"][name] = {
                "prefix_length": f"{low}" if low == high else f"{low}-{high}",
                "n": len(times),
                "errors": errors,
                "empty_answers": empty,
                "p50_ms": round(percentile(times, 50), 1),
                "p95_ms": round(percentile(times, 95), 1),
                "p99_ms": round(percentile(times, 99), 1),
                "max_ms": round(max(times), 1),
                "mean_ms": round(statistics.fmean(times), 1),
                "p95_ms_by_types": {k: round(percentile(v, 95), 1) for k, v in by_class.items()},
            }
    print(json.dumps(summary, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, "rows": rows}, handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
