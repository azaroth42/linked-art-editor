"""Per-process state of a ``lae bootstrap`` worker: its event loop, connection and settings.

``lae.bootstrap.load`` and ``lae.bootstrap.rewrite`` start their process pools with
``init_worker``; the functions they run in the pool call ``current``.
"""

from __future__ import annotations

import asyncio
from multiprocessing.synchronize import Semaphore
from typing import Any
from uuid import UUID

import asyncpg


class Worker:
    """What one worker process keeps between batches."""

    loop: asyncio.AbstractEventLoop
    conn: asyncpg.Connection
    base_uri: str
    context_version: int
    label_language: str
    """``Settings.label_language``: the language of the ``entity_labels`` default labels."""
    draft_epoch: bool
    """Whether ``records.draft_epoch`` exists (Step 1.8)."""
    run_id: UUID
    """Id of this run: ``qa_findings.sweep_run_id`` of the findings it writes."""
    write_slots: Semaphore
    """At most ``--streams`` workers write at the same time (Spike S6)."""

    def run(self, awaitable: Any) -> Any:
        """Run a coroutine on the worker's loop and return its result."""
        return self.loop.run_until_complete(awaitable)


_WORKER: Worker | None = None


def init_worker(
    dsn: str,
    base_uri: str,
    context_version: int,
    label_language: str,
    draft_epoch: bool,
    run_id: UUID,
    write_slots: Semaphore,
) -> None:
    """Pool initializer: open the worker's connection and keep its settings."""
    global _WORKER
    worker = Worker()
    worker.loop = asyncio.new_event_loop()
    worker.conn = worker.loop.run_until_complete(asyncpg.connect(dsn))
    worker.base_uri = base_uri
    worker.context_version = context_version
    worker.label_language = label_language
    worker.draft_epoch = draft_epoch
    worker.run_id = run_id
    worker.write_slots = write_slots
    _WORKER = worker


def current() -> Worker:
    """Return this process's worker state (the pool must have been started with ``init_worker``)."""
    assert _WORKER is not None, "this function runs in a pool started with init_worker"
    return _WORKER
