"""The single-instance lock of ``crdt-ws`` (decision A4: ``crdt-ws`` is a single process).

Rooms live in the memory of one process. A second process on the same database would hold its
own ``Doc`` for the same draft, and each one's compaction would replace ``crdt_drafts.state`` and
delete ``crdt_updates`` rows that the other one's ``Doc`` does not contain: edits would be lost
without any error (``tests/crdt/test_instance.py`` shows it). The epoch guard does not help,
because both processes hold the current epoch.

``InstanceLock`` therefore takes a PostgreSQL session-level advisory lock on a connection of its
own and keeps that connection for as long as the process serves:

- ``acquire`` waits a short while for a predecessor that is still shutting down (a restart) and
  then raises ``InstanceLockError``; ``lae crdt-ws`` does not start.
- PostgreSQL releases the lock when the connection ends, however the process dies, so a killed
  process never blocks its successor.
- A watchdog checks the connection. If it is lost (a database restart), the lock is gone and
  another process may take it, so ``on_lost`` is awaited: the server drops its rooms without
  writing anything and terminates; the service manager starts it again.

The lock is per database. It needs a direct connection or a session-pooling proxy (a
transaction-pooling PgBouncer would give the lock to whatever session it picked).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from lae.logging import get_logger

INSTANCE_LOCK_KEY: Final = 0x4C41455F43524454
"""The advisory lock key: the bytes ``LAE_CRDT`` as a 64-bit integer."""

ACQUIRE_SECONDS: Final = 30.0
"""How long ``acquire`` waits for another holder to let go before it gives up."""

RETRY_SECONDS: Final = 0.5

CHECK_SECONDS: Final = 5.0
"""Interval at which the watchdog checks that the lock's connection is alive."""

logger = get_logger(__name__)


class InstanceLockError(RuntimeError):
    """Another ``crdt-ws`` process holds the lock on this database."""


class InstanceLock:
    """A session-level advisory lock held on a dedicated connection of ``engine``."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        on_lost: Callable[[], Awaitable[None]],
        acquire_seconds: float = ACQUIRE_SECONDS,
        check_seconds: float = CHECK_SECONDS,
    ) -> None:
        self._engine = engine
        self._on_lost = on_lost
        self._acquire_seconds = acquire_seconds
        self._check_seconds = check_seconds
        self._connection: AsyncConnection | None = None
        self._watchdog: asyncio.Task[None] | None = None

    @property
    def held(self) -> bool:
        """Whether the lock was acquired and has not been released or lost."""
        return self._connection is not None

    async def acquire(self) -> None:
        """Take the lock, waiting up to ``acquire_seconds``; raise ``InstanceLockError`` if held."""
        logger.info("instance_lock_connecting")
        connection = await self._engine.connect()
        try:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._acquire_seconds
            waiting = False
            while not await connection.scalar(select(func.pg_try_advisory_lock(INSTANCE_LOCK_KEY))):
                if loop.time() >= deadline:
                    raise InstanceLockError(
                        "another crdt-ws process is serving this database (decision A4: "
                        "exactly one may run); stop it first"
                    )
                if not waiting:
                    waiting = True
                    logger.warning("instance_lock_waiting", seconds=self._acquire_seconds)
                await asyncio.sleep(RETRY_SECONDS)
        except BaseException:
            await _discard(connection)
            raise
        self._connection = connection
        self._watchdog = asyncio.create_task(self._watch(connection), name="instance-lock")
        logger.info("instance_lock_acquired")

    async def release(self) -> None:
        """Give the lock up (ending its connection releases it)."""
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
        connection, self._connection = self._connection, None
        if connection is not None:
            # Invalidate, not close: a connection returned to the pool would keep the lock.
            await _discard(connection)

    async def _watch(self, connection: AsyncConnection) -> None:
        while True:
            await asyncio.sleep(self._check_seconds)
            try:
                await connection.scalar(select(1))
            except Exception as exc:
                logger.critical("instance_lock_lost", error=repr(exc))
                self._connection = None
                await _discard(connection)
                await self._on_lost()
                return


async def _discard(connection: AsyncConnection) -> None:
    """End the database session of ``connection`` instead of returning it to the pool."""
    with contextlib.suppress(Exception):
        await connection.invalidate()
    with contextlib.suppress(Exception):
        await connection.close()
