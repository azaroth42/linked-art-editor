"""Decision B5 with a real process: an update that was persisted survives a kill before broadcast.

``crash_server.py`` is the ``crdt-ws`` application in its own process. It is told to send itself
``SIGKILL`` after the second update is committed to ``crdt_updates`` and before that update is
applied or broadcast. A second process started afterwards must serve the update.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from lae.projection import ops
from tests.auth.conftest import create_token, create_user
from tests.crdt.helpers import (
    INTERNAL_TOKEN,
    SESSION_SECRET,
    YClient,
    create_record,
    draft_row,
    eventually,
    free_port,
    update_count,
)

CRASH_SERVER = Path(__file__).with_name("crash_server.py")


@asynccontextmanager
async def crdt_process(
    db_url: str, port: int, die_at: int
) -> AsyncIterator[asyncio.subprocess.Process]:
    """Run ``crash_server.py`` until the block ends (or it kills itself)."""
    env = {
        **os.environ,
        "LAE_DATABASE_URL": db_url,
        "LAE_ENV": "test",
        "LAE_INTERNAL_TOKEN": INTERNAL_TOKEN,
        "LAE_SESSION_SECRET": SESSION_SECRET,
        "LAE_DEV_USER": "",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(CRASH_SERVER),
        str(port),
        str(die_at),
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient() as http:

            async def live() -> bool:
                assert process.returncode is None, "crdt-ws exited during startup"
                try:
                    return (await http.get(f"http://127.0.0.1:{port}/health/live")).is_success
                except httpx.HTTPError:
                    return False

            await eventually(live, seconds=30, what="crdt-ws process is live")
        yield process
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 20)
            except TimeoutError:
                process.kill()
                await process.wait()


def add_name(content: str) -> Any:
    return lambda doc: ops.add_node(doc, "identified_by", {"type": "Name", "content": content})


def names(client: YClient) -> list[str]:
    return [name["content"] for name in client.project()["identified_by"]]


async def test_update_persisted_before_a_kill_survives_the_restart(
    db_url: str, db_engine: AsyncEngine
) -> None:
    record_id, _ = await create_record(db_engine)
    user_id = await create_user(db_engine, "Editor")
    token, _ = await create_token(db_engine, user_id, "edit_draft")
    port = free_port()
    url = f"ws://127.0.0.1:{port}/ws/rooms/{record_id}?token={token}&epoch=0"
    writer, peer = YClient(url), YClient(url)

    async with crdt_process(db_url, port, die_at=2) as process:
        await writer.connect()
        await peer.connect()
        await writer.wait_synced()
        await peer.wait_synced()

        add_name("Broadcast normally")(writer.doc)
        await eventually(
            lambda: "Broadcast normally" in names(peer), what="first update at the peer"
        )
        assert await update_count(db_engine, record_id) == 1

        add_name("Persisted, then killed")(writer.doc)  # the process dies inside this write
        returncode = await asyncio.wait_for(process.wait(), 20)
        assert returncode == -signal.SIGKILL
        await writer.wait_closed(10)
        await peer.wait_closed(10)
        await writer.close()
        await peer.close()

    # Persisted: the row is there. Not broadcast: the peer never received it.
    assert await update_count(db_engine, record_id) == 2
    assert names(peer) == ["First name", "Broadcast normally"]
    assert peer.updates_received == 1
    row = await draft_row(db_engine, record_id)
    assert row is not None and row.contributors == [user_id]

    async with crdt_process(db_url, port, die_at=0):
        # A client that has never seen the record gets the update from the restarted process...
        async with YClient(url) as newcomer:
            await newcomer.wait_synced()
            assert names(newcomer) == ["First name", "Broadcast normally", "Persisted, then killed"]
        # ... and so does the peer that was connected when the process died.
        await peer.connect()
        await peer.wait_synced()
        assert names(peer) == ["First name", "Broadcast normally", "Persisted, then killed"]
        await peer.close()
        # The writer resends what it has; nothing is duplicated.
        await writer.connect()
        await writer.wait_synced()
        await asyncio.sleep(0.3)
        assert names(writer) == ["First name", "Broadcast normally", "Persisted, then killed"]
        await writer.close()
        async with YClient(url) as check:
            await check.wait_synced()
            assert names(check) == ["First name", "Broadcast normally", "Persisted, then killed"]
