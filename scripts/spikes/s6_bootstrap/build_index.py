"""Index stage: build a QLever index from one ``.nt`` file with ``deploy/Qleverfile``; check it.

``index`` runs ``qlever index`` (Homebrew ``qlever-index``, settings from the Qleverfile:
on-disk-compressed vocabulary, parallel parsing, text index from literals, ``ql:has-word``
triples) in a fresh directory under a unique ``--name``, samples the RSS of the whole process
tree with ``ps``, and reads the phase timestamps from the index log.

``serve-check`` starts that index on ``--port`` with ``PERSIST_UPDATES``, counts the triples, runs
one text query, and stops exactly that server (never a bare ``qlever stop``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Final

import httpx

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s6_bootstrap.common import RssSampler, write_summary  # noqa: E402

QLEVERFILE: Final = Path(__file__).resolve().parents[3] / "deploy" / "Qleverfile"
LOG_LINE: Final = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+) - (\w+): (.*)$")


def _qlever(*args: str) -> list[str]:
    return ["qlever", "--qleverfile", str(QLEVERFILE), *args]


def _dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.iterdir() if p.is_file() and not p.is_symlink())


def _log_events(log: Path) -> list[tuple[float, str]]:
    """``(seconds since the first line, message)`` for each INFO line of a QLever log."""
    events: list[tuple[float, str]] = []
    first: float | None = None
    for line in log.read_text(errors="replace").splitlines():
        match = LOG_LINE.match(line)
        if match is None:
            continue
        stamp = time.mktime(time.strptime(match[1][:19], "%Y-%m-%d %H:%M:%S"))
        stamp += float("0." + match[1][20:])
        first = stamp if first is None else first
        events.append((round(stamp - first, 1), match[3]))
    return events


def run_index(args: argparse.Namespace) -> dict[str, Any]:
    """Build the index and return its summary."""
    nt = args.nt.resolve()
    workdir: Path = args.dir
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    # qlever-control globs INPUT_FILES relative to the index directory (absolute patterns
    # raise "Non-relative patterns are unsupported"), so link the input in.
    (workdir / nt.name).symlink_to(nt)
    env = dict(os.environ, QLEVER_INPUT_FILES=nt.name)
    cmd = _qlever("index", "--name", args.name)
    if args.stxxl_memory:
        cmd += ["--stxxl-memory", args.stxxl_memory]
    started = time.perf_counter()
    with subprocess.Popen(
        cmd, cwd=workdir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    ) as proc:
        with RssSampler(proc.pid, interval=0.5) as sampler:
            output, _ = proc.communicate()
    seconds = time.perf_counter() - started
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    (workdir / "qlever-index-command-output.txt").write_bytes(output)
    log = workdir / f"{args.name}.index-log.txt"
    events = _log_events(log) if log.exists() else []
    meta_path = workdir / f"{args.name}.meta-data.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    usage = workdir / f"{args.name}.index.resource-usage-log.tsv"
    usage_rss = (
        [int(row.split("\t")[2]) for row in usage.read_text().splitlines()[1:]]
        if (usage.exists())
        else []
    )
    return {
        "stage": "index",
        "nt": str(nt),
        "nt_bytes": nt.stat().st_size,
        "name": args.name,
        "dir": str(workdir),
        "returncode": proc.returncode,
        "seconds": round(seconds, 1),
        "triples_parsed": _number(events, r"Triples parsed: ([\d,]+)"),
        "triples_distinct": meta.get("num-triples", {}).get("normal"),
        "has_word_triples": _number(events, r"`ql:has-word` triples created: ([\d,]+)"),
        "blank_nodes": meta.get("num-blank-nodes-total"),
        "phases_s": _phases(events),
        "peak_rss_gib_index_binary_log": round(max(usage_rss, default=0) / 2**30, 2),
        "peak_rss_gib_ps_tree": round(sampler.peak_tree_kib / 2**20, 2),
        "peak_rss_gib_ps_single": round(sampler.peak_single_kib / 2**20, 2),
        "peak_rss_ps_single_command": sampler.peak_single_command,
        "children_maxrss_gib": round(children.ru_maxrss / 2**30, 2),
        "children_cpu_s": round(children.ru_utime + children.ru_stime, 1),
        "index_bytes": _dir_bytes(workdir),
        "log_events": events,
    }


PHASES: Final = (
    ("parse", "Parsing input triples", "Merging partial vocabularies"),
    ("vocabulary_merge", "Merging partial vocabularies", "Converting triples from local IDs"),
    ("permutations", "Converting triples from local IDs", "Index build completed"),
    ("text_index", "Adding text index", "Text index build completed"),
)


def _phases(events: list[tuple[float, str]]) -> dict[str, float]:
    """Durations of the main index-builder phases, from the log's start/end markers."""
    found: dict[str, float] = {}
    for name, start, end in PHASES:
        t0 = next((t for t, m in events if m.startswith(start)), None)
        t1 = next((t for t, m in events if m.startswith(end)), None)
        if t0 is not None and t1 is not None:
            found[name] = round(t1 - t0, 1)
    if events:
        found["total_log"] = events[-1][0]
    return found


def _number(events: list[tuple[float, str]], pattern: str) -> int | None:
    """The number in the last log message matching ``pattern`` (progress lines come first)."""
    found = None
    for _, message in events:
        match = re.search(pattern, message)
        if match:
            found = int(match[1].replace(",", ""))
    return found


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _sparql(port: int, query: str) -> dict[str, Any]:
    response = httpx.post(
        f"http://localhost:{port}",
        data={"query": query},
        headers={"Accept": "application/sparql-results+json"},
        timeout=600,
    )
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


def run_serve_check(args: argparse.Namespace) -> dict[str, Any]:
    """Start the index on ``--port``, count triples, run a text query, stop the server."""
    if not _port_free(args.port):
        raise SystemExit(f"port {args.port} is in use; pick another (agent-workflow §3)")
    env = dict(os.environ, QLEVER_PORT=str(args.port))
    started = time.perf_counter()
    subprocess.run(
        _qlever("start", "--name", args.name, "--port", str(args.port)),
        cwd=args.dir,
        env=env,
        check=True,
        capture_output=True,
    )
    for _ in range(600):
        try:
            if httpx.get(f"http://localhost:{args.port}/ping", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    ready_s = time.perf_counter() - started
    try:
        t0 = time.perf_counter()
        count = _sparql(args.port, "SELECT (COUNT(*) AS ?c) WHERE { ?s ?p ?o }")
        count_s = time.perf_counter() - t0
        triples = int(count["results"]["bindings"][0]["c"]["value"])
        t0 = time.perf_counter()
        text = _sparql(
            args.port,
            "PREFIX ql: <http://qlever.cs.uni-freiburg.de/builtin-functions/> "
            'SELECT (COUNT(?t) AS ?c) WHERE { ?t ql:contains-word "portrait*" }',
        )
        text_s = time.perf_counter() - t0
        text_hits = int(text["results"]["bindings"][0]["c"]["value"])
        pids = subprocess.run(
            ["lsof", "-t", "-i", f":{args.port}", "-sTCP:LISTEN"], capture_output=True, text=True
        ).stdout.split()
        rss_kib = 0
        for pid in pids:
            out = subprocess.run(["ps", "-o", "rss=", "-p", pid], capture_output=True, text=True)
            rss_kib += int(out.stdout.strip() or 0)
    finally:
        # Only this server: its index name is unique to the slot, and the port is checked too.
        subprocess.run(
            _qlever(
                "stop",
                "--name",
                args.name,
                "--no-containers",
                "--cmdline-regex",
                rf"qlever-server .*-i [^ ]*{args.name} .*-p {args.port}\b",
            ),
            cwd=args.dir,
            check=False,
            capture_output=True,
        )
    return {
        "stage": "serve-check",
        "port": args.port,
        "ready_s": round(ready_s, 1),
        "triples": triples,
        "count_query_s": round(count_s, 2),
        "text_query_hits": text_hits,
        "text_query_s": round(text_s, 2),
        "server_rss_gib": round(rss_kib / 2**20, 2),
        "stopped": _port_free(args.port),
    }


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run ``index`` or ``serve-check``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)
    index = sub.add_parser("index", help="build the index")
    index.add_argument("--nt", type=Path, required=True)
    index.add_argument("--dir", type=Path, required=True, help="fresh index directory")
    index.add_argument("--name", default="lae_i11")
    index.add_argument("--stxxl-memory", help="override the Qleverfile's STXXL_MEMORY")
    index.add_argument("--stats-out", type=Path)
    serve = sub.add_parser("serve-check", help="start, count, text query, stop")
    serve.add_argument("--dir", type=Path, required=True)
    serve.add_argument("--name", default="lae_i11")
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--stats-out", type=Path)
    args = parser.parse_args(argv)
    summary = run_index(args) if args.command == "index" else run_serve_check(args)
    write_summary(summary, args.stats_out)
    return 0 if summary.get("returncode", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
