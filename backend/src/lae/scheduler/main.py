"""``lae scheduler``: run the registered jobs on their schedules until ``SIGTERM``.

A simple asyncio loop (no scheduling library): one task per job, so a job never overlaps itself
in this process and different jobs run independently. Each task works out the next run from the
job's ``sync_state`` record (``lae.scheduler.jobs``), sleeps until then, and calls ``run_job``,
which takes the job's advisory lock, so two scheduler processes never run the same job at once.

Timing per job:

- at start, ``last_run`` is read from ``sync_state``: a restart does not repeat an hourly job
  that ran ten minutes ago, and a job that missed its time while the process was down runs
  once at once;
- after a run, the next one is ``schedule.next_run(start of that run)``;
- when another process holds the lock, ``last_run`` is read again (that process wrote its
  start) and the next attempt is at least ``RETRY_SECONDS`` away;
- when the database cannot be reached, the next attempt is ``RETRY_SECONDS`` away (or the
  regular time, if that is sooner).

Shutdown: ``SIGTERM`` or ``SIGINT`` sets ``JobContext.stopping``. No new run starts; a running job
sees ``ctx.should_stop()`` and returns after its current item; after ``SHUTDOWN_GRACE_SECONDS``
whatever still runs is cancelled (and recorded as ``interrupted``). Then the HTTP client and the
engine are closed and ``main`` returns 0. The systemd unit's default ``TimeoutStopSec`` (90 s) is
longer than the grace period. Should a job's loop itself fail (a bug, not a failing run, which is
only recorded), the process stops the same way and exits 1, so the service manager restarts it.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Final

import httpx

from lae.crdt.server import EXIT_CONFIG_ERROR, TOKEN_REQUIRED_ENVS
from lae.db.session import create_engine, create_sessionmaker
from lae.logging import get_logger
from lae.scheduler import reaper
from lae.scheduler.jobs import DailyAt, Every, Job, JobContext, read_job_state, run_job, utcnow
from lae.settings import Settings
from lae.vocab import usage

SHUTDOWN_GRACE_SECONDS: Final = 30.0
"""How long running jobs may take to finish after ``SIGTERM`` before they are cancelled."""

RETRY_SECONDS: Final = 60.0
"""Delay before another attempt when the lock was held elsewhere or the database was down."""

HTTP_TIMEOUT_SECONDS: Final = 30.0

EXIT_JOB_LOOP_CRASHED: Final = 1
"""Exit code when a job's loop failed unexpectedly (systemd's ``Restart=on-failure`` restarts)."""

STOP_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)

logger = get_logger(__name__)


def registered_jobs(settings: Settings) -> list[Job]:
    """Return every job this process runs.

    - ``reap_drafts``, hourly: the draft reaper (``lae.scheduler.reaper``, decision B7).
    - ``refresh_usage_counts``, nightly: ``entity_labels.usage_count`` from QLever
      (``lae.vocab.usage``, Phase 2 Step 2.1).
    """
    return [
        Job(reaper.JOB_NAME, reaper.run, Every(reaper.INTERVAL)),
        Job(usage.JOB_NAME, usage.run_job, DailyAt(usage.JOB_TIME)),
    ]


class Scheduler:
    """Runs ``jobs`` on their schedules until ``ctx.stopping`` is set."""

    def __init__(
        self,
        ctx: JobContext,
        jobs: Sequence[Job],
        *,
        clock: Callable[[], datetime] = utcnow,
        grace_seconds: float = SHUTDOWN_GRACE_SECONDS,
        retry_seconds: float = RETRY_SECONDS,
    ) -> None:
        names = [job.name for job in jobs]
        if len(set(names)) != len(names):
            raise ValueError(f"job names must be unique: {names}")
        self.ctx = ctx
        self.jobs = list(jobs)
        self._clock = clock
        self._grace_seconds = grace_seconds
        self._retry = timedelta(seconds=retry_seconds)
        self.crashed = False
        """Set when a job's loop ended with an unexpected error (the process then stops)."""

    async def run(self) -> None:
        """Run until ``ctx.stopping`` is set, then let running jobs finish (or cancel them)."""
        tasks = [
            asyncio.create_task(self._guarded_loop(job), name=f"job:{job.name}")
            for job in self.jobs
        ]
        try:
            await self.ctx.stopping.wait()
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=self._grace_seconds)
                for task in pending:
                    logger.warning("job_cancelled_at_shutdown", job=task.get_name())
                    task.cancel()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _sleep_until(self, due: datetime) -> bool:
        """Sleep until ``due``; return ``False`` (at once) when the process is stopping."""
        delay = (due - self._clock()).total_seconds()
        if self.ctx.should_stop():
            return False
        if delay <= 0:
            return True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.ctx.stopping.wait(), timeout=delay)
        return not self.ctx.should_stop()

    async def _last_run(self, job: Job) -> datetime | None:
        state = await read_job_state(self.ctx.sessions, job.name)
        return state.last_run

    async def _guarded_loop(self, job: Job) -> None:
        """``_loop``; if it fails unexpectedly, stop the whole process (to be restarted)."""
        try:
            await self._loop(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.critical("job_loop_crashed", job=job.name, exc_info=True)
            self.crashed = True
            self.ctx.stopping.set()

    async def _loop(self, job: Job) -> None:
        """Run one job on its schedule until the process stops."""
        last_run: datetime | None = None
        not_before: datetime | None = None
        try:
            last_run = await self._last_run(job)
        except Exception:
            logger.exception("job_state_unreadable", job=job.name)
            not_before = self._clock() + self._retry
        while not self.ctx.should_stop():
            now = self._clock()
            due = job.schedule.next_run(last_run, now)
            if not_before is not None and due < not_before:
                due = not_before
            logger.debug("job_scheduled", job=job.name, next_run=due.isoformat())
            if not await self._sleep_until(due):
                return
            not_before = None
            try:
                state = await run_job(job, self.ctx, clock=self._clock)
                if state is None:  # another process holds the lock and has recorded its start
                    last_run = await self._last_run(job)
                    not_before = self._clock() + self._retry
                else:
                    last_run = state.last_run
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("job_run_failed", job=job.name)
                not_before = self._clock() + self._retry


def _config_error(settings: Settings) -> str | None:
    """Why the scheduler must not start with these settings, else ``None``."""
    if settings.internal_token is None and settings.env in TOKEN_REQUIRED_ENVS:
        return (
            f"LAE_INTERNAL_TOKEN is required when LAE_ENV={settings.env} "
            "(the reaper discards drafts through crdt-ws)"
        )
    return None


async def main(settings: Settings, *, jobs: Sequence[Job] | None = None) -> int:
    """``lae scheduler``: run the registered jobs until ``SIGTERM`` or ``SIGINT``; return 0."""
    problem = _config_error(settings)
    if problem is not None:
        print(f"lae scheduler: refusing to start: {problem}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    to_run = registered_jobs(settings) if jobs is None else list(jobs)
    engine = create_engine(settings.database_url)
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    def stop(signum: signal.Signals) -> None:
        if not stopping.is_set():
            logger.info("scheduler_stopping", signal=signum.name)
        stopping.set()

    for signum in STOP_SIGNALS:
        loop.add_signal_handler(signum, stop, signum)
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as http:
            ctx = JobContext(
                settings=settings,
                engine=engine,
                sessions=create_sessionmaker(engine),
                http=http,
                stopping=stopping,
            )
            logger.info(
                "scheduler_started",
                jobs=[job.name for job in to_run],
                draft_ttl_days=settings.draft_ttl_days,
            )
            scheduler = Scheduler(ctx, to_run)
            await scheduler.run()
    finally:
        for signum in STOP_SIGNALS:
            loop.remove_signal_handler(signum)
        await engine.dispose()
    logger.info("scheduler_stopped", crashed=scheduler.crashed)
    return EXIT_JOB_LOOP_CRASHED if scheduler.crashed else 0
