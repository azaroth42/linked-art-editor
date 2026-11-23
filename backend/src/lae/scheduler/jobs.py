"""Scheduler jobs: their schedules, one run under an advisory lock, and the ``sync_state`` record.

**Bookkeeping.** Every job has one ``sync_state`` row, key ``job:<name>``, whose JSON value is a
``JobState``: ``last_run`` (when the last run started, ISO 8601 UTC), ``last_status``
(``running`` while a run is in progress, then ``ok``, ``error`` or ``interrupted``),
``duration`` (seconds the last run took), ``last_error``, ``result`` (the summary the job
returned) and ``next_run``. ``last_run`` is written when a run *starts*, so another scheduler
process, and this one after a restart, schedule from it. A ``running`` status that outlives its
process (killed with ``SIGKILL``) is overwritten by the next run. The admin dashboard of Phase 2
Step 2.17 reads these rows with ``read_job_state``.

**At most one run of a job at a time, across processes.** ``run_job`` first takes the
session-level PostgreSQL advisory lock ``job_lock_key(name)`` with ``pg_try_advisory_lock`` on a
connection of its own; if another process holds it, the run is skipped (``run_job`` returns
``None``). The connection is closed (never returned to the pool) when the run ends, which
releases the lock however the run ended; a process that dies releases it with its connection.
If that connection is lost during a run (a database restart), the lock is gone too, so jobs must
tolerate a second run overlapping the end of the first: the reaper re-checks every draft under
row locks before it acts. Like ``crdt-ws``'s instance lock, this needs a direct connection or a
session-pooling proxy.

**Stopping.** ``JobContext.stopping`` is set on ``SIGTERM``; a job checks ``ctx.should_stop()``
between items of work and returns early. A job still running when the shutdown grace period
ends is cancelled and recorded as ``interrupted``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time as monotonic_time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any, Final, Literal, Protocol

import httpx
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from lae.crdt.client import CrdtWsClient
from lae.db.models import SyncState
from lae.logging import get_logger
from lae.settings import Settings

JOB_KEY_PREFIX: Final = "job:"
"""``sync_state`` keys of job records are ``job:<name>``."""

LOCK_KEY_PREFIX: Final = "lae.scheduler.job:"
"""Hashed with the job name into the job's advisory lock key."""

MAX_ERROR_LENGTH: Final = 1000
"""``last_error`` is cut to this many characters."""

STATE_WRITE_SECONDS: Final = 5.0
"""How long recording an interrupted run may take during shutdown."""

JobStatus = Literal["running", "ok", "error", "interrupted"]

logger = get_logger(__name__)


def utcnow() -> datetime:
    """The current time, timezone-aware UTC."""
    return datetime.now(UTC)


class Schedule(Protocol):
    """When a job runs next."""

    def next_run(self, last_run: datetime | None, now: datetime) -> datetime:
        """Return when to run next, given the start of the last run (``None``: never ran).

        A time at or before ``now`` means "now": a run missed while the scheduler was down is
        made up once, not once per missed slot.
        """
        ...


@dataclass(frozen=True, slots=True)
class Every:
    """Run every ``interval``, counted from the start of the last run; at once if never run."""

    interval: timedelta

    def next_run(self, last_run: datetime | None, now: datetime) -> datetime:
        """``last_run + interval``, or ``now`` for a job that never ran."""
        return now if last_run is None else last_run + self.interval


@dataclass(frozen=True, slots=True)
class DailyAt:
    """Run once a day at ``at`` (UTC); a job that never ran waits for the next such time."""

    at: time

    def next_run(self, last_run: datetime | None, now: datetime) -> datetime:
        """The first ``at`` after the last run (after ``now`` for a job that never ran)."""
        base = (now if last_run is None else last_run).astimezone(UTC)
        candidate = datetime.combine(base.date(), self.at.replace(tzinfo=None), tzinfo=UTC)
        return candidate if candidate > base else candidate + timedelta(days=1)


@dataclass(slots=True)
class JobContext:
    """What a job may use: settings, the database, the HTTP client, and the stop signal."""

    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    http: httpx.AsyncClient
    stopping: asyncio.Event = field(default_factory=asyncio.Event)

    def should_stop(self) -> bool:
        """Whether the process is shutting down; check between items of work."""
        return self.stopping.is_set()

    def crdt(self) -> CrdtWsClient:
        """The ``crdt-ws`` internal RPC client (``LAE_CRDT_WS_URL``, ``LAE_INTERNAL_TOKEN``)."""
        return CrdtWsClient.from_settings(self.settings, self.http)


JobFunction = Callable[[JobContext], Awaitable[Mapping[str, Any] | None]]
"""A job: returns a small JSON-serializable summary (stored as ``result``) or ``None``."""


@dataclass(frozen=True, slots=True)
class Job:
    """A registered job: its name (``sync_state`` key ``job:<name>``), function and schedule."""

    name: str
    run: JobFunction
    schedule: Schedule


class JobState(BaseModel):
    """The JSON value of a job's ``sync_state`` row."""

    last_run: datetime | None = None
    """When the last (or current) run started."""
    last_status: JobStatus | None = None
    duration: float | None = None
    """Seconds the last finished run took."""
    last_error: str | None = None
    result: dict[str, Any] | None = None
    """What the last run returned."""
    next_run: datetime | None = None
    """When this scheduler plans the next run (written when a run ends)."""


def job_key(name: str) -> str:
    """The ``sync_state`` key of a job's record."""
    return JOB_KEY_PREFIX + name


def job_lock_key(name: str) -> int:
    """The job's advisory lock key: a signed 64-bit hash of its name."""
    digest = hashlib.blake2b((LOCK_KEY_PREFIX + name).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def read_job_state(sessions: async_sessionmaker[AsyncSession], name: str) -> JobState:
    """Return the job's record from ``sync_state`` (an empty one if it never ran)."""
    async with sessions() as session:
        value = await session.scalar(select(SyncState.value).where(SyncState.key == job_key(name)))
    return JobState() if value is None else JobState.model_validate(value)


async def write_job_state(
    sessions: async_sessionmaker[AsyncSession], name: str, state: JobState
) -> None:
    """Upsert the job's record in ``sync_state``."""
    value = state.model_dump(mode="json")
    statement = insert(SyncState).values(key=job_key(name), value=value)
    statement = statement.on_conflict_do_update(
        index_elements=[SyncState.key], set_={"value": statement.excluded.value}
    )
    async with sessions() as session, session.begin():
        await session.execute(statement)


async def _discard(connection: AsyncConnection) -> None:
    """End the database session of ``connection`` (and so its advisory locks); never pool it."""
    with contextlib.suppress(Exception):
        await connection.invalidate()
    with contextlib.suppress(Exception):
        await connection.close()


@contextlib.asynccontextmanager
async def job_lock(engine: AsyncEngine, name: str) -> AsyncIterator[bool]:
    """Try the job's advisory lock on a dedicated connection; yield whether it was acquired.

    The connection is discarded on exit, which releases the lock in every case.
    """
    connection = await engine.connect()
    try:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        acquired = await connection.scalar(select(func.pg_try_advisory_lock(job_lock_key(name))))
        yield bool(acquired)
    finally:
        await _discard(connection)


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]


async def run_job(
    job: Job, ctx: JobContext, *, clock: Callable[[], datetime] = utcnow
) -> JobState | None:
    """Run ``job`` once under its advisory lock and record it in ``sync_state``.

    Returns the recorded ``JobState``, or ``None`` if another process holds the lock (nothing is
    run or written). An exception from the job is logged and recorded as ``error``; it is not
    raised. Failures of the database itself (the lock, the bookkeeping) are raised.
    Cancellation (the shutdown grace period ran out) is recorded as ``interrupted`` and
    re-raised.
    """
    async with job_lock(ctx.engine, job.name) as acquired:
        if not acquired:
            logger.info("job_skipped_locked", job=job.name)
            return None
        previous = await read_job_state(ctx.sessions, job.name)
        started = clock()
        await write_job_state(
            ctx.sessions,
            job.name,
            previous.model_copy(update={"last_run": started, "last_status": "running"}),
        )
        logger.info("job_started", job=job.name)
        began = monotonic_time.monotonic()

        def finished(status: JobStatus, **fields: Any) -> JobState:
            return JobState(
                last_run=started,
                last_status=status,
                duration=round(monotonic_time.monotonic() - began, 3),
                next_run=job.schedule.next_run(started, clock()),
                **fields,
            )

        try:
            result = await job.run(ctx)
        except asyncio.CancelledError:
            state = finished("interrupted", last_error="cancelled at shutdown")
            logger.warning("job_interrupted", job=job.name, duration=state.duration)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    write_job_state(ctx.sessions, job.name, state), STATE_WRITE_SECONDS
                )
            raise
        except Exception as exc:
            state = finished("error", last_error=_error_text(exc))
            logger.exception("job_failed", job=job.name, duration=state.duration)
        else:
            state = finished("ok", result=None if result is None else dict(result))
            logger.info("job_finished", job=job.name, duration=state.duration, result=state.result)
        await write_job_state(ctx.sessions, job.name, state)
        return state
