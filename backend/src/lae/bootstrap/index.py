"""``lae bootstrap build-index``: build a QLever index from an export and start serving it.

Runs ``qlever index`` with ``deploy/Qleverfile`` (on-disk-compressed vocabulary, parallel
parsing, text index from literals, N-Quads input) in a fresh directory, then ``qlever start
--persist-updates`` so that the sync worker's updates survive restarts (spec §3.3). qlever-control
globs its input relative to the index directory and rejects absolute patterns ("Non-relative
patterns are unsupported", Spike S6 finding 4), so the export is symlinked into the directory.

When the server answers, ``sync_state`` gets ``qlever_index_watermark``, the highest outbox event
id the export reflects (from ``<file>.meta.json``, written by ``export-nt``): the sync worker
replays every later event. ``bootstrap_context_version`` and ``bootstrap_expander_version`` are
set to the versions the export expanded with, because those produced the triples now in QLever
(ADR-0007; ``lae bootstrap load`` sets them first).

The server is left running. To stop it, match its index name and port (``qlever stop
--cmdline-regex``, as ``make down`` does); never run a bare ``qlever stop``, which stops every
QLever server on the machine.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import resource
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import asyncpg
import httpx

from lae.bootstrap.common import (
    BOOTSTRAP_CONTEXT_VERSION,
    BOOTSTRAP_EXPANDER_VERSION,
    QLEVER_INDEX_WATERMARK,
    asyncpg_dsn,
    set_sync_state,
)
from lae.bootstrap.export import meta_path
from lae.settings import Settings

QLEVERFILE: Final = Path(__file__).resolve().parents[4] / "deploy" / "Qleverfile"
DEFAULT_NAME: Final = "lae"
"""Index name: ``[data] NAME`` of ``deploy/Qleverfile``."""
DEFAULT_PORT: Final = 7010
"""Port when neither ``--port`` nor ``LAE_QLEVER_URL`` gives one (``deploy/Qleverfile``)."""
READY_TIMEOUT_S: Final = 300.0
COUNT_QUERY: Final = "SELECT (COUNT(*) AS ?c) WHERE { ?s ?p ?o }"
ACCESS_TOKEN_ENV: Final = "LAE_QLEVER_ACCESS_TOKEN"
INPUT_FILES_ENV: Final = "QLEVER_INPUT_FILES"


class IndexBuildError(RuntimeError):
    """The index could not be built or served."""


def qlever_command(*args: str) -> list[str]:
    """``qlever --qleverfile deploy/Qleverfile <args>``."""
    return ["qlever", "--qleverfile", str(QLEVERFILE), *args]


def stop_command(name: str, port: int) -> list[str]:
    """The command that stops exactly the server of index ``name`` on ``port``, no other."""
    regex = rf"^\S*qlever-server -i {re.escape(name)} .*-p {port} "
    return qlever_command("stop", "--name", name, "--no-containers", "--cmdline-regex", regex)


def port_of(url: str | None) -> int | None:
    """The port of a QLever URL such as ``http://localhost:7010``, or ``None``."""
    if not url:
        return None
    return urlsplit(url).port


def port_in_use(port: int) -> bool:
    """True if something accepts TCP connections on ``localhost:port``."""
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def read_meta(nt: Path) -> dict[str, Any] | None:
    """The ``export-nt`` metadata written next to ``nt``, if there is one."""
    path = meta_path(nt)
    if not path.is_file():
        return None
    meta: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return meta


def sparql_count(port: int) -> int:
    """Run ``SELECT (COUNT(*) AS ?c) WHERE { ?s ?p ?o }`` and return the count."""
    response = httpx.post(
        f"http://localhost:{port}",
        data={"query": COUNT_QUERY},
        headers={"Accept": "application/sparql-results+json"},
        timeout=600,
    )
    response.raise_for_status()
    return int(response.json()["results"]["bindings"][0]["c"]["value"])


def _wait_until_ready(port: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://localhost:{port}/ping", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise IndexBuildError(f"QLever did not answer on port {port} within {timeout_s:.0f} s")


def children_peak_rss_gib() -> float:
    """Peak RSS of the largest finished child process (the index builder), in GiB."""
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return round(peak / (2**30 if sys.platform == "darwin" else 2**20), 2)


def _tail(text: bytes, lines: int = 20) -> str:
    return "\n".join(text.decode("utf-8", "replace").splitlines()[-lines:])


def _prepare_directory(index_dir: Path, nt: Path) -> None:
    if index_dir.exists() and any(index_dir.iterdir()):
        raise IndexBuildError(
            f"{index_dir} is not empty; build-index needs a fresh directory "
            "(a running server may be using the index in it)"
        )
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / nt.name).symlink_to(nt)


def _environment(nt: Path, access_token: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env[INPUT_FILES_ENV] = nt.name
    env.pop(ACCESS_TOKEN_ENV, None)
    if access_token:
        env[ACCESS_TOKEN_ENV] = access_token
    return env


async def _record_state(database_url: str, values: dict[str, Any]) -> None:
    conn = await asyncpg.connect(asyncpg_dsn(database_url))
    try:
        await set_sync_state(conn, values)
    finally:
        await conn.close()


def build_index(
    nt: Path,
    *,
    settings: Settings,
    index_dir: Path,
    name: str = DEFAULT_NAME,
    port: int | None = None,
    watermark: int | None = None,
    ready_timeout_s: float = READY_TIMEOUT_S,
) -> dict[str, Any]:
    """Build the index, start the server, record the watermark; return a summary.

    ``watermark`` overrides the one in ``<nt>.meta.json``; one of the two is required. ``port``
    defaults to the port of ``LAE_QLEVER_URL``, else 7010. Raises ``IndexBuildError``.
    """
    nt = nt.resolve()
    if not nt.is_file():
        raise IndexBuildError(f"no such export: {nt}")
    meta = read_meta(nt)
    if watermark is None:
        if meta is None:
            raise IndexBuildError(
                f"{meta_path(nt)} not found: pass --watermark (the max outbox_events.id "
                "at the time of the export)"
            )
        watermark = int(meta["watermark"])
    port = port or port_of(settings.qlever_url) or DEFAULT_PORT
    if port_in_use(port):
        raise IndexBuildError(f"port {port} is already in use; stop that server or pick another")
    token = settings.qlever_access_token
    access_token = token.get_secret_value() if token is not None else None
    env = _environment(nt, access_token)
    _prepare_directory(index_dir, nt)

    started = time.perf_counter()
    index = subprocess.run(
        qlever_command("index", "--name", name),
        cwd=index_dir,
        env=env,
        capture_output=True,
        check=False,
    )
    index_s = time.perf_counter() - started
    peak_rss_gib = children_peak_rss_gib()
    (index_dir / "qlever-index-command-output.txt").write_bytes(index.stdout + index.stderr)
    if index.returncode != 0:
        raise IndexBuildError(f"qlever index failed:\n{_tail(index.stdout + index.stderr)}")
    index_meta_file = index_dir / f"{name}.meta-data.json"
    index_meta = json.loads(index_meta_file.read_text()) if index_meta_file.is_file() else {}

    started = time.perf_counter()
    start = subprocess.run(
        qlever_command("start", "--name", name, "--port", str(port), "--persist-updates"),
        cwd=index_dir,
        env=env,
        capture_output=True,
        check=False,
    )
    if start.returncode != 0:
        raise IndexBuildError(f"qlever start failed:\n{_tail(start.stdout + start.stderr)}")
    _wait_until_ready(port, ready_timeout_s)
    ready_s = time.perf_counter() - started
    triples = sparql_count(port)

    state: dict[str, Any] = {QLEVER_INDEX_WATERMARK: watermark}
    if meta is not None:
        state[BOOTSTRAP_CONTEXT_VERSION] = meta["context_version"]
        state[BOOTSTRAP_EXPANDER_VERSION] = meta["expander_version"]
    asyncio.run(_record_state(settings.database_url, state))
    return {
        "stage": "build-index",
        "nt": str(nt),
        "nt_bytes": nt.stat().st_size,
        "dir": str(index_dir),
        "name": name,
        "port": port,
        "index_seconds": round(index_s, 1),
        "triples_distinct": index_meta.get("num-triples", {}).get("normal"),
        "peak_rss_gib": peak_rss_gib,
        "index_bytes": sum(
            p.stat().st_size for p in index_dir.iterdir() if p.is_file() and not p.is_symlink()
        ),
        "ready_seconds": round(ready_s, 1),
        "triples_served": triples,
        "update_access_token": access_token is not None,
        "sync_state": state,
        "stop_command": shlex.join(stop_command(name, port)),
    }
