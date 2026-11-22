"""Helpers shared by the S6 stages: database DSN, process CPU and RSS sampling, summaries."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from lae.settings import get_settings


def pg_dsn() -> str:
    """``LAE_DATABASE_URL`` as a plain libpq/asyncpg DSN."""
    url: str = get_settings().database_url
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def cpu_seconds(pid: int) -> float:
    """User+system CPU seconds of a running process, from ``ps`` (``[[h:]m:]s.ss``)."""
    out = subprocess.run(
        ["ps", "-o", "cputime=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    if not out:
        return 0.0
    seconds = 0.0
    for part in out.replace("-", ":").split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


class RssSampler:
    """Samples the RSS of a process tree with ``ps`` until stopped; keeps the peaks."""

    def __init__(self, root_pid: int, interval: float = 0.25) -> None:
        self.root_pid = root_pid
        self.interval = interval
        self.peak_tree_kib = 0
        self.peak_single_kib = 0
        self.peak_single_command = ""
        self.samples: list[tuple[float, int]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = time.perf_counter()

    def __enter__(self) -> RssSampler:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.interval)

    def sample(self) -> None:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,rss=,comm="], capture_output=True, text=True
        ).stdout
        children: dict[int, list[int]] = {}
        info: dict[int, tuple[int, str]] = {}
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
            children.setdefault(ppid, []).append(pid)
            info[pid] = (rss, parts[3])
        tree, stack = [], [self.root_pid]
        while stack:
            pid = stack.pop()
            if pid in info:
                tree.append(pid)
            stack.extend(children.get(pid, ()))
        total = sum(info[pid][0] for pid in tree)
        self.samples.append((time.perf_counter() - self._started, total))
        self.peak_tree_kib = max(self.peak_tree_kib, total)
        for pid in tree:
            rss, command = info[pid]
            if rss > self.peak_single_kib:
                self.peak_single_kib = rss
                self.peak_single_command = command


def write_summary(summary: dict[str, Any], path: Path | None) -> None:
    """Print the stage summary as JSON and, if given, write it to ``path``."""
    text = json.dumps(summary, indent=2)
    print(text)
    if path is not None:
        path.write_text(text + "\n")
