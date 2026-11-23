"""The job runner (``lae.scheduler.jobs`` and the ``Scheduler`` loop of ``lae.scheduler.main``).

Jobs here are fakes with unique names (``sync_state`` is shared by every test), run against the
test database: the advisory lock and the ``job:<name>`` bookkeeping are real.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, time, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from uuid_utils.compat import uuid7

from lae.crdt.instance import INSTANCE_LOCK_KEY
from lae.db.models import SyncState
from lae.scheduler import reaper
from lae.scheduler.jobs import (
    DailyAt,
    Every,
    Job,
    JobContext,
    JobState,
    job_key,
    job_lock_key,
    read_job_state,
    run_job,
    write_job_state,
)
from lae.scheduler.main import Scheduler, registered_jobs
from lae.settings import Settings

T0 = datetime(2026, 11, 22, 10, 30, tzinfo=UTC)


def unique_name(prefix: str) -> str:
    return f"test_{prefix}_{uuid7().hex[:12]}"


@pytest.fixture
async def ctx(db_url: str, db_engine: AsyncEngine) -> AsyncIterator[JobContext]:
    """A job context on the test database (no ``crdt-ws``)."""
    async with httpx.AsyncClient() as http:
        yield JobContext(
            settings=Settings(database_url=db_url),
            engine=db_engine,
            sessions=async_sessionmaker(db_engine, expire_on_commit=False),
            http=http,
        )


async def wait_for(predicate: Any, seconds: float = 5.0) -> None:
    """Poll ``predicate`` (sync or async) until it is true, or fail."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return
        if loop.time() > deadline:
            pytest.fail("condition not reached in time")
        await asyncio.sleep(0.02)


# Schedules -------------------------------------------------------------------------------------


def test_every_runs_at_once_the_first_time_then_after_the_interval() -> None:
    hourly = Every(timedelta(hours=1))
    assert hourly.next_run(None, T0) == T0
    assert hourly.next_run(T0 - timedelta(minutes=10), T0) == T0 + timedelta(minutes=50)
    overdue = hourly.next_run(T0 - timedelta(days=2), T0)
    assert overdue <= T0  # missed runs are made up once, at once


def test_daily_at_waits_for_its_time() -> None:
    nightly = DailyAt(time(3, 0))
    assert nightly.next_run(None, T0) == datetime(2026, 11, 23, 3, 0, tzinfo=UTC)
    early = datetime(2026, 11, 22, 1, 0, tzinfo=UTC)
    assert nightly.next_run(None, early) == datetime(2026, 11, 22, 3, 0, tzinfo=UTC)
    ran = datetime(2026, 11, 22, 3, 0, 5, tzinfo=UTC)
    assert nightly.next_run(ran, T0) == datetime(2026, 11, 23, 3, 0, tzinfo=UTC)
    missed = datetime(2026, 11, 19, 3, 0, 2, tzinfo=UTC)
    assert nightly.next_run(missed, T0) <= T0


def test_lock_keys_are_stable_distinct_64_bit_integers() -> None:
    assert job_lock_key("reap_drafts") == job_lock_key("reap_drafts")
    assert job_lock_key("reap_drafts") != job_lock_key("refresh_usage_counts")
    assert job_lock_key("reap_drafts") != INSTANCE_LOCK_KEY
    assert -(2**63) <= job_lock_key("reap_drafts") < 2**63
    assert job_key("reap_drafts") == "job:reap_drafts"


def test_the_reaper_is_registered_hourly() -> None:
    jobs = {job.name: job for job in registered_jobs(Settings())}
    assert set(jobs) == {"reap_drafts", "refresh_usage_counts"}
    assert jobs["refresh_usage_counts"].schedule == DailyAt(time(3, 0))
    assert jobs["reap_drafts"].run is reaper.run
    assert jobs["reap_drafts"].schedule == Every(timedelta(hours=1))


def test_job_names_must_be_unique(ctx: JobContext) -> None:
    async def nothing(_: JobContext) -> None:
        return None

    job = Job("twice", nothing, Every(timedelta(hours=1)))
    with pytest.raises(ValueError, match="unique"):
        Scheduler(ctx, [job, job])


# One run ---------------------------------------------------------------------------------------


async def test_a_run_is_recorded_in_sync_state(ctx: JobContext) -> None:
    async def count(_: JobContext) -> Mapping[str, Any]:
        return {"counted": 3}

    name = unique_name("ok")
    job = Job(name, count, Every(timedelta(hours=1)))
    state = await run_job(job, ctx, clock=lambda: T0)

    assert state is not None
    stored = await read_job_state(ctx.sessions, name)
    assert stored == state
    assert stored.last_run == T0
    assert stored.last_status == "ok"
    assert stored.duration is not None and stored.duration >= 0
    assert stored.result == {"counted": 3}
    assert stored.last_error is None
    assert stored.next_run == T0 + timedelta(hours=1)


async def test_a_failing_job_is_recorded_as_error(ctx: JobContext) -> None:
    async def boom(_: JobContext) -> None:
        raise ValueError("boom")

    name = unique_name("error")
    state = await run_job(Job(name, boom, Every(timedelta(minutes=5))), ctx)
    assert state is not None
    assert state.last_status == "error"
    assert state.last_error == "ValueError: boom"
    assert (await read_job_state(ctx.sessions, name)).last_status == "error"


async def test_a_job_never_runs_twice_at_once(ctx: JobContext, db_engine: AsyncEngine) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    runs = 0

    async def slow(_: JobContext) -> None:
        nonlocal runs
        runs += 1
        entered.set()
        await release.wait()

    job = Job(unique_name("lock"), slow, Every(timedelta(hours=1)))
    first = asyncio.create_task(run_job(job, ctx))
    await asyncio.wait_for(entered.wait(), 5)
    # A second scheduler (another connection, as another process would have) is refused.
    assert await run_job(job, ctx) is None
    state = await read_job_state(ctx.sessions, job.name)
    assert state.last_status == "running"
    release.set()
    finished = await asyncio.wait_for(first, 5)
    assert finished is not None and finished.last_status == "ok"
    assert runs == 1
    # The lock went with its connection: the next run gets it.
    assert await run_job(job, ctx) is not None
    assert runs == 2


async def test_the_lock_is_released_when_the_job_fails(ctx: JobContext) -> None:
    async def boom(_: JobContext) -> None:
        raise RuntimeError("x")

    job = Job(unique_name("release"), boom, Every(timedelta(hours=1)))
    assert await run_job(job, ctx) is not None
    assert await run_job(job, ctx) is not None


# The loop --------------------------------------------------------------------------------------


async def test_scheduler_runs_jobs_on_their_interval_until_stopped(ctx: JobContext) -> None:
    runs: list[datetime] = []

    async def tick(_: JobContext) -> Mapping[str, Any]:
        runs.append(datetime.now(UTC))
        return {"n": len(runs)}

    job = Job(unique_name("tick"), tick, Every(timedelta(seconds=0.2)))
    task = asyncio.create_task(Scheduler(ctx, [job]).run())
    await wait_for(lambda: len(runs) >= 3)
    ctx.stopping.set()
    await asyncio.wait_for(task, 5)
    count = len(runs)
    await asyncio.sleep(0.4)
    assert len(runs) == count  # nothing runs after the stop
    assert runs[1] - runs[0] >= timedelta(seconds=0.19)
    state = await read_job_state(ctx.sessions, job.name)
    assert state.last_status == "ok" and state.result == {"n": count}


async def test_a_restart_schedules_from_the_recorded_last_run(ctx: JobContext) -> None:
    runs = 0

    async def tick(_: JobContext) -> None:
        nonlocal runs
        runs += 1

    recent = Job(unique_name("recent"), tick, Every(timedelta(hours=1)))
    overdue = Job(unique_name("overdue"), tick, Every(timedelta(hours=1)))
    now = datetime.now(UTC)
    await write_job_state(ctx.sessions, recent.name, JobState(last_run=now - timedelta(minutes=10)))
    await write_job_state(ctx.sessions, overdue.name, JobState(last_run=now - timedelta(hours=3)))

    task = asyncio.create_task(Scheduler(ctx, [recent, overdue]).run())
    await wait_for(lambda: runs >= 1)
    await asyncio.sleep(0.3)
    ctx.stopping.set()
    await asyncio.wait_for(task, 5)

    assert runs == 1  # only the overdue job
    assert (await read_job_state(ctx.sessions, overdue.name)).last_status == "ok"
    assert (await read_job_state(ctx.sessions, recent.name)).last_status is None


async def test_a_job_that_sees_the_stop_finishes_its_run(ctx: JobContext) -> None:
    started = asyncio.Event()

    async def careful(job_ctx: JobContext) -> Mapping[str, Any]:
        started.set()
        await job_ctx.stopping.wait()
        return {"stopped_early": job_ctx.should_stop()}

    job = Job(unique_name("careful"), careful, Every(timedelta(hours=1)))
    task = asyncio.create_task(Scheduler(ctx, [job], grace_seconds=5).run())
    await asyncio.wait_for(started.wait(), 5)
    ctx.stopping.set()
    await asyncio.wait_for(task, 5)
    state = await read_job_state(ctx.sessions, job.name)
    assert state.last_status == "ok"
    assert state.result == {"stopped_early": True}


async def test_a_job_still_running_after_the_grace_period_is_interrupted(
    ctx: JobContext,
) -> None:
    started = asyncio.Event()

    async def stubborn(_: JobContext) -> None:
        started.set()
        await asyncio.sleep(3600)

    job = Job(unique_name("stubborn"), stubborn, Every(timedelta(hours=1)))
    task = asyncio.create_task(Scheduler(ctx, [job], grace_seconds=0.2).run())
    await asyncio.wait_for(started.wait(), 5)
    ctx.stopping.set()
    await asyncio.wait_for(task, 5)
    state = await read_job_state(ctx.sessions, job.name)
    assert state.last_status == "interrupted"
    assert state.last_error == "cancelled at shutdown"
    # And the lock was released with its connection.
    assert await run_job(Job(job.name, _noop, job.schedule), ctx) is not None


async def test_a_lock_held_elsewhere_is_retried_later(
    ctx: JobContext, db_engine: AsyncEngine
) -> None:
    runs = 0

    async def tick(_: JobContext) -> None:
        nonlocal runs
        runs += 1

    job = Job(unique_name("held"), tick, Every(timedelta(seconds=0.1)))
    async with db_engine.connect() as holder:
        await holder.execute(select(func.pg_advisory_lock(job_lock_key(job.name))))
        task = asyncio.create_task(Scheduler(ctx, [job], retry_seconds=0.2).run())
        await asyncio.sleep(0.5)
        assert runs == 0
        await holder.execute(select(func.pg_advisory_unlock(job_lock_key(job.name))))
        await wait_for(lambda: runs >= 1)
    ctx.stopping.set()
    await asyncio.wait_for(task, 5)


async def _noop(_: JobContext) -> None:
    return None


async def test_job_state_round_trips_through_jsonb(ctx: JobContext) -> None:
    name = unique_name("json")
    state = JobState(
        last_run=T0,
        last_status="ok",
        duration=1.5,
        result={"a": [1, 2]},
        next_run=T0 + timedelta(hours=1),
    )
    await write_job_state(ctx.sessions, name, state)
    await write_job_state(ctx.sessions, name, state.model_copy(update={"duration": 2.0}))
    async with AsyncSession(ctx.engine) as session:
        value = await session.scalar(select(SyncState.value).where(SyncState.key == job_key(name)))
    assert value["last_run"] == "2026-11-22T10:30:00Z"
    assert value["duration"] == 2.0
    assert value["last_status"] == "ok"


class BrokenSchedule:
    """A schedule with a bug: the job's loop itself fails."""

    def next_run(self, last_run: datetime | None, now: datetime) -> datetime:
        raise RuntimeError("bug")


async def test_a_crashed_job_loop_stops_the_scheduler(ctx: JobContext) -> None:
    healthy = Job(unique_name("healthy"), _noop, Every(timedelta(hours=1)))
    broken = Job(unique_name("broken"), _noop, BrokenSchedule())
    scheduler = Scheduler(ctx, [healthy, broken])
    await asyncio.wait_for(scheduler.run(), 5)
    assert scheduler.crashed
    assert ctx.should_stop()
