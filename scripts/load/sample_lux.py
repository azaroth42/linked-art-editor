"""Draw a deterministic sample of records from the local LUX dump for the Phase 1 scale gate.

``scripts/load/bootstrap_1m.sh`` runs it (Step 1.22). The dump is a directory of
``export_full_*.jsonl.gz`` files; every line wraps one Linked Art record in a ``json`` member
(next to ``triples``, ``admin`` and ``indexedProperties``, which are dropped). The output is
plain JSON Lines with one bare record per line, which ``lae bootstrap load --input`` reads.

**Selection.** Every record gets a 64-bit key, the keyed BLAKE2b hash of its ``id``. The sample
is the ``--count`` records with the smallest keys (bottom-k sampling): it does not depend on file
order or on the number of worker processes, every record has the same chance, and so each record
type is represented in proportion to its share of the dump. Records whose id appears twice are
counted once (the first occurrence in file order is kept).

One pass over the dump: each file is decompressed by ``gzip -dc`` in its own worker process (the
command-line decompressor is several times faster than Python's), and every line whose key is
below ``--fraction`` of the key space is kept as a candidate in ``<out>.parts/``. The candidates
are then cut down to exactly ``--count``. ``gzip`` checks every member's CRC and length, so a
truncated or corrupt file makes the run fail (exit 2): the dump is not complete, and the caller
falls back to the S6 generator.

Never fetch LUX online (``docs/agent-workflow.md`` §2); this reads only the local files.

Usage::

    python scripts/load/sample_lux.py --dump /path/to/dataset/jsonl --count 1000000 \\
        --out /tmp/lae-i35/lux-1m.jsonl [--workers 12] [--fraction 0.04]

Prints a JSON summary (also written to ``<out>.manifest.json``).
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Final

KEY: Final = b"lae-step-1.22"
"""BLAKE2b key: changing it draws a different (equally valid) sample."""

KEY_SPACE: Final = 1 << 64
EXIT_INCOMPLETE: Final = 2
"""Exit code when the dump is missing, unreadable or too small for ``--count``."""

_HEAD: Final = re.compile(rb'^\{"json":\{"id":"([^"]+)","type":"([^"]+)"')
"""The dump writes ``id`` and ``type`` first; anything else is parsed in full."""


def record_key(record_id: bytes) -> int:
    """The record's 64-bit sampling key."""
    return int.from_bytes(hashlib.blake2b(record_id, digest_size=8, key=KEY).digest(), "big")


def _head(line: bytes) -> tuple[bytes, bytes]:
    match = _HEAD.match(line)
    if match:
        return match.group(1), match.group(2)
    record = json.loads(line)["json"]
    return str(record["id"]).encode(), str(record.get("type", "?")).encode()


def _body(line: bytes) -> bytes:
    """The bare record of one dump line, as compact JSON."""
    record = json.loads(line)["json"]
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()


def scan_file(path: str, part: str, threshold: int) -> dict[str, Any]:
    """Write the candidates of one dump file to ``part``, one ``key id type record`` per line.

    The four fields are separated by tabs; ``key`` is hexadecimal.
    """
    started = time.monotonic()
    lines = 0
    candidates = 0
    types: Counter[str] = Counter()
    with (
        subprocess.Popen(["gzip", "-dc", path], stdout=subprocess.PIPE, bufsize=1 << 20) as gz,
        open(part, "wb") as out,
    ):
        assert gz.stdout is not None
        for line in gz.stdout:
            if len(line) < 3:
                continue
            lines += 1
            record_id, record_type = _head(line)
            types[record_type.decode()] += 1
            key = record_key(record_id)
            if key < threshold:
                candidates += 1
                out.write(b"%016x\t%s\t%s\t%s\n" % (key, record_id, record_type, _body(line)))
    if gz.returncode != 0:
        raise RuntimeError(f"gzip -dc {path} exited with {gz.returncode}: file incomplete")
    return {
        "file": Path(path).name,
        "lines": lines,
        "candidates": candidates,
        "types": dict(types),
        "seconds": round(time.monotonic() - started, 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dump", type=Path, required=True, help="directory of *.jsonl.gz")
    parser.add_argument("--count", type=int, default=1_000_000)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument(
        "--fraction",
        type=float,
        default=0.04,
        help="share of the key space kept as candidates (must yield at least --count)",
    )
    args = parser.parse_args(argv)

    files = sorted(args.dump.glob("*.jsonl.gz"))
    if not files:
        print(f"sample_lux: no *.jsonl.gz in {args.dump}", file=sys.stderr)
        return EXIT_INCOMPLETE
    parts = args.out.with_name(args.out.name + ".parts")
    shutil.rmtree(parts, ignore_errors=True)
    parts.mkdir(parents=True)
    threshold = int(KEY_SPACE * args.fraction)
    started = time.monotonic()

    scans: list[dict[str, Any]] = []
    with ProcessPoolExecutor(args.workers) as pool:
        futures = {
            pool.submit(scan_file, str(f), str(parts / f"{i:03d}.tsv"), threshold): f
            for i, f in enumerate(files)
        }
        try:
            for future in as_completed(futures):
                scans.append(future.result())
        except Exception as error:  # noqa: BLE001 - any failure means the dump is not usable
            print(f"sample_lux: {error}", file=sys.stderr)
            for pending in futures:
                pending.cancel()
            return EXIT_INCOMPLETE
    scan_seconds = time.monotonic() - started

    # The --count smallest distinct keys over all candidates (ids collide only on duplicates).
    part_files = sorted(parts.glob("*.tsv"))
    seen: dict[bytes, int] = {}
    for part in part_files:
        with part.open("rb") as lines:
            for line in lines:
                key_hex, record_id, _ = line.split(b"\t", 2)
                seen.setdefault(record_id, int(key_hex, 16))
    if len(seen) < args.count:
        print(
            f"sample_lux: only {len(seen)} candidates for --count {args.count}; "
            "raise --fraction or use the generator",
            file=sys.stderr,
        )
        return EXIT_INCOMPLETE
    cutoff = heapq.nsmallest(args.count, seen.values())[-1]
    duplicates = sum(s["candidates"] for s in scans) - len(seen)

    sample_types: Counter[str] = Counter()
    written: set[bytes] = set()
    digest = hashlib.sha256()
    with args.out.open("wb") as out:
        for part in part_files:
            with part.open("rb") as lines:
                for line in lines:
                    key_hex, record_id, record_type, body = line.split(b"\t", 3)
                    if int(key_hex, 16) > cutoff or record_id in written:
                        continue
                    written.add(record_id)
                    out.write(body)
                    digest.update(body)
                    sample_types[record_type.decode()] += 1
    shutil.rmtree(parts)

    dump_types: Counter[str] = Counter()
    for scan in scans:
        dump_types.update(scan["types"])
    summary = {
        "source": "lux",
        "dump": str(args.dump),
        "files": len(files),
        "dump_records": sum(s["lines"] for s in scans),
        "dump_types": dict(dump_types.most_common()),
        "duplicate_ids_in_candidates": duplicates,
        "count": len(written),
        "sample_types": dict(sample_types.most_common()),
        "key": KEY.decode(),
        "out": str(args.out),
        "out_bytes": args.out.stat().st_size,
        "out_sha256": digest.hexdigest(),
        "scan_seconds": round(scan_seconds, 1),
        "seconds": round(time.monotonic() - started, 1),
        "slowest_file_seconds": max(s["seconds"] for s in scans),
    }
    text = json.dumps(summary, indent=2)
    args.out.with_name(args.out.name + ".manifest.json").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
