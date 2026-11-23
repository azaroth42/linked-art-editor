"""``lae scheduler`` as a process: it starts, runs its jobs, and stops cleanly on ``SIGTERM``."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lae.db.models import SyncState
from lae.scheduler import reaper
from lae.scheduler.jobs import job_key, read_job_state
from lae.scheduler.main import main
from lae.settings import Settings

START_SECONDS = 60.0
"""Generous: the subprocess imports the whole package before it logs anything."""


async def read_events(stream: asyncio.StreamReader, until: str, seen: list[dict[str, Any]]) -> None:
    """Collect JSON log lines from ``stream`` until one has ``event == until``."""
    while True:
        line = await stream.readline()
        if not line:
            pytest.fail(f"the process ended before logging {until!r}: {seen}")
        try:
            event = json.loads(line)
        except ValueError:
            continue
        seen.append(event)
        if event.get("event") == until:
            return


async def test_sigterm_stops_the_process_cleanly(db_url: str, db_engine: AsyncEngine) -> None:
    async with AsyncSession(db_engine) as session, session.begin():
        # The reaper has then never run: it runs at once.
        await session.execute(delete(SyncState).where(SyncState.key == job_key(reaper.JOB_NAME)))
    env = {key: value for key, value in os.environ.items() if not key.startswith("LAE_")}
    env.update(
        {
            "LAE_DATABASE_URL": db_url,
            "LAE_ENV": "test",
            "LAE_LOG_LEVEL": "info",
            # Nothing listens there: the reaper falls back to the database if it finds a draft.
            "LAE_CRDT_WS_URL": "http://127.0.0.1:9",
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lae.cli",
        "scheduler",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None
    seen: list[dict[str, Any]] = []
    try:
        await asyncio.wait_for(
            read_events(process.stdout, "scheduler_started", seen), START_SECONDS
        )
        await asyncio.wait_for(read_events(process.stdout, "job_finished", seen), START_SECONDS)
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(read_events(process.stdout, "scheduler_stopped", seen), 30)
        code = await asyncio.wait_for(process.wait(), 30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert code == 0
    started = next(e for e in seen if e["event"] == "scheduler_started")
    assert started["jobs"] == ["reap_drafts", "refresh_usage_counts"]
    assert any(e["event"] == "scheduler_stopping" and e["signal"] == "SIGTERM" for e in seen)
    state = await read_job_state(async_sessionmaker(db_engine), reaper.JOB_NAME)
    assert state.last_status == "ok"
    assert state.result is not None and state.result["failed"] == 0


async def test_refuses_to_start_without_the_internal_token_in_production(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        env="production",
        internal_token=None,
        database_url="postgresql+asyncpg://lae@127.0.0.1:1/unused",
    )
    assert await main(settings) == 2
    assert "LAE_INTERNAL_TOKEN is required" in capsys.readouterr().err
