"""``lae bootstrap <load|rewrite-refs|export-nt|build-index|aat-labels>``: parsing and dispatch.

The stage modules are imported only when their sub-command runs, so ``lae --help`` and the other
commands do not pay for pyld or the JSON Schema registry.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from lae.settings import Settings, get_settings

EXIT_FAILED = 1
"""Exit code when a stage could not complete (the reason is printed on stderr)."""

SUBCOMMANDS: dict[str, str] = {
    "load": "Stream Linked Art JSON-LD into records and entity_labels with COPY.",
    "rewrite-refs": "Rewrite references to re-minted records in every bulk-loaded record.",
    "export-nt": "Expand every active record (and sidecar labels) into one N-Triples file.",
    "build-index": "Build a QLever index from an export and start the server.",
    "aat-labels": "Seed entity_labels with Getty AAT terms from a CSV.",
}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the ``bootstrap`` sub-commands and their options to ``parser``."""
    sub = parser.add_subparsers(dest="stage", required=True, metavar="<stage>")

    load = sub.add_parser("load", help=SUBCOMMANDS["load"], description=SUBCOMMANDS["load"])
    load.add_argument("--input", type=Path, required=True, help="directory or JSON Lines file")
    load.add_argument(
        "--report",
        type=Path,
        default=Path("bootstrap-load-report.jsonl"),
        help="JSON Lines file listing the records that were not loaded, and why",
    )
    load.add_argument("--batch", type=int, default=10_000, help="records per COPY batch")
    load.add_argument("--streams", type=int, default=4, help="concurrent COPY connections")
    load.add_argument("--workers", type=int, help="worker processes (default: CPU count)")

    rewrite = sub.add_parser(
        "rewrite-refs", help=SUBCOMMANDS["rewrite-refs"], description=SUBCOMMANDS["rewrite-refs"]
    )
    rewrite.add_argument("--batch", type=int, default=10_000, help="records per worker task")
    rewrite.add_argument("--streams", type=int, default=4, help="concurrent writing connections")
    rewrite.add_argument("--workers", type=int, help="worker processes (default: CPU count)")

    export = sub.add_parser(
        "export-nt", help=SUBCOMMANDS["export-nt"], description=SUBCOMMANDS["export-nt"]
    )
    export.add_argument(
        "--out", type=Path, required=True, help="output file; gzip-compressed if it ends in .gz"
    )
    export.add_argument("--workers", type=int, help="worker processes (default: CPU count)")
    export.add_argument("--batch", type=int, default=500, help="records per worker task")
    export.add_argument("--report", type=Path, help="default: <out>.report.jsonl")

    index = sub.add_parser(
        "build-index", help=SUBCOMMANDS["build-index"], description=SUBCOMMANDS["build-index"]
    )
    index.add_argument("--nt", type=Path, required=True, help="file written by export-nt")
    index.add_argument(
        "--dir", type=Path, required=True, help="index directory; must not exist or be empty"
    )
    index.add_argument("--name", default="lae", help="index name (default: lae)")
    index.add_argument("--port", type=int, help="server port (default: from LAE_QLEVER_URL)")
    index.add_argument("--watermark", type=int, help="override the watermark in <nt>.meta.json")

    aat = sub.add_parser(
        "aat-labels", help=SUBCOMMANDS["aat-labels"], description=SUBCOMMANDS["aat-labels"]
    )
    aat.add_argument("--csv", type=Path, required=True, help="linkedart-parser aat_dataset.csv")
    aat.add_argument("--report", type=Path, default=Path("bootstrap-aat-labels-report.jsonl"))


def _run_stage(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    stage: str = args.stage
    if stage == "load":
        from lae.bootstrap.load import load

        return asyncio.run(
            load(
                args.input,
                settings=settings,
                report_path=args.report,
                batch=args.batch,
                streams=args.streams,
                workers=args.workers,
            )
        )
    if stage == "rewrite-refs":
        from lae.bootstrap.rewrite import rewrite_refs

        return asyncio.run(
            rewrite_refs(
                settings=settings, batch=args.batch, streams=args.streams, workers=args.workers
            )
        )
    if stage == "export-nt":
        from lae.bootstrap.export import export_nt

        return asyncio.run(
            export_nt(
                args.out,
                settings=settings,
                workers=args.workers,
                batch=args.batch,
                report_path=args.report,
            )
        )
    if stage == "build-index":
        from lae.bootstrap.index import build_index

        return build_index(
            args.nt,
            settings=settings,
            index_dir=args.dir,
            name=args.name,
            port=args.port,
            watermark=args.watermark,
        )
    if stage == "aat-labels":
        from lae.bootstrap.aat import load_aat_labels

        return asyncio.run(load_aat_labels(args.csv, settings=settings, report_path=args.report))
    raise AssertionError(f"unknown bootstrap stage {stage!r}")


def run(args: argparse.Namespace) -> int:
    """Run the selected stage, print its JSON summary; return the exit code."""
    from lae.bootstrap.common import BootstrapError, print_summary
    from lae.bootstrap.index import IndexBuildError

    try:
        summary = _run_stage(args, get_settings())
    except (BootstrapError, IndexBuildError, FileNotFoundError) as error:
        print(f"lae bootstrap {args.stage}: {error}", file=sys.stderr)
        return EXIT_FAILED
    print_summary(summary)
    return 0
