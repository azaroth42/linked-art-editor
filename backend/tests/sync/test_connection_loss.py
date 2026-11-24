"""What ``run`` does when a database error ends it (issue #151; contract §3 item 22, §4).

A lost connection (the server shut the session down, the socket is gone, a commit failed that
way) is logged ``sync_connection_lost`` and the process exits 1 for its supervisor to restart.
Any other database error (a bug in our SQL, a violated constraint) is not taken for one: it is
logged ``sync_worker_failed`` with its traceback, and the process exits 1 as well. Either way
nothing is recorded for the batch in flight, so the restarted worker sends it again.

Errors are injected two ways: by raising the error class (every class the worker must recognise,
whether or not this machine's Postgres produces it here), and for real, by terminating the
worker's backend from a second connection with ``pg_terminate_backend``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from lae.bootstrap.common import asyncpg_dsn
from lae.logging import configure_logging
from lae.sync import worker
from lae.sync.worker import (
    BATCH_SIZE,
    EXIT_FAILURE,
    ConnectionLostError,
    connection_lost,
    process_batch,
)
from tests.publish.conftest import Caller
from tests.sync.conftest import (
    Network,
    RecordingClient,
    Served,
    all_events,
    triples_of,
    watermark,
)
from tests.sync.test_batch import World, five_events, worker_settings

pytestmark = pytest.mark.usefixtures("queue")

Logged = Callable[[], list[dict[str, Any]]]

# -- the classification -------------------------------------------------------------------------

SESSION_ENDED: list[Callable[[], BaseException]] = [
    lambda: asyncpg.AdminShutdownError("terminating connection due to administrator command"),
    lambda: asyncpg.CrashShutdownError("terminating connection because of crash of another"),
    lambda: asyncpg.CannotConnectNowError("the database system is shutting down"),
    lambda: asyncpg.DatabaseDroppedError("terminating connection: the database was dropped"),
    lambda: asyncpg.IdleSessionTimeoutError("terminating connection due to idle-session timeout"),
    lambda: asyncpg.IdleInTransactionSessionTimeoutError("idle-in-transaction timeout"),
    lambda: asyncpg.TransactionTimeoutError("terminating connection due to transaction timeout"),
    lambda: asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation"),
    lambda: asyncpg.ConnectionFailureError("connection failure"),
    lambda: asyncpg.TransactionResolutionUnknownError("transaction resolution unknown"),
    lambda: ConnectionLostError("the database connection is closed"),
    lambda: ConnectionResetError("connection reset by peer"),
    lambda: TimeoutError(),
    lambda: asyncpg.PostgresError.new({"C": "57P99", "S": "FATAL", "V": "FATAL", "M": "gone"}),
]
SESSION_ENDED_IDS = [
    "admin_shutdown",
    "crash_shutdown",
    "cannot_connect_now",
    "database_dropped",
    "idle_session_timeout",
    "idle_in_transaction_timeout",
    "transaction_timeout",
    "connection_does_not_exist",
    "connection_failure",
    "transaction_resolution_unknown",
    "connection_lost_error",
    "socket_reset",
    "socket_timeout",
    "unknown_fatal",
]

NOT_LOST: list[Callable[[], BaseException]] = [
    lambda: asyncpg.UndefinedColumnError('column "no_such_column" does not exist'),
    lambda: asyncpg.PostgresSyntaxError('syntax error at or near "SELEC"'),
    lambda: asyncpg.UndefinedTableError('relation "no_such_table" does not exist'),
    lambda: asyncpg.UniqueViolationError("duplicate key value violates unique constraint"),
    lambda: asyncpg.ForeignKeyViolationError("violates foreign key constraint"),
    lambda: asyncpg.NotNullViolationError("violates not-null constraint"),
    lambda: asyncpg.CheckViolationError("violates check constraint"),
    lambda: asyncpg.InvalidTextRepresentationError("invalid input syntax for type json"),
    lambda: asyncpg.QueryCanceledError("canceling statement due to statement timeout"),
    lambda: asyncpg.SerializationError("could not serialize access"),
    lambda: asyncpg.DeadlockDetectedError("deadlock detected"),
    lambda: asyncpg.exceptions._base.DataError("invalid input for query argument $1"),
    lambda: asyncpg.InterfaceError("cannot perform operation: another operation is in progress"),
    lambda: asyncpg.PostgresError.new({"C": "XX000", "S": "ERROR", "V": "ERROR", "M": "oops"}),
]
NOT_LOST_IDS = [
    "undefined_column",
    "syntax_error",
    "undefined_table",
    "unique_violation",
    "foreign_key_violation",
    "not_null_violation",
    "check_violation",
    "invalid_text",
    "query_canceled",
    "serialization_failure",
    "deadlock",
    "client_data_error",
    "interface_error_on_an_open_connection",
    "internal_error",
]


@pytest.mark.parametrize("make", SESSION_ENDED, ids=SESSION_ENDED_IDS)
def test_a_session_the_server_ended_is_a_lost_connection(
    make: Callable[[], BaseException],
) -> None:
    assert connection_lost(make())
    assert connection_lost(make(), closed=True)


@pytest.mark.parametrize("make", NOT_LOST, ids=NOT_LOST_IDS)
def test_a_bug_or_a_violated_constraint_is_not_a_lost_connection(
    make: Callable[[], BaseException],
) -> None:
    assert not connection_lost(make())


def test_an_interface_error_is_a_lost_connection_only_when_the_connection_is_closed() -> None:
    error = asyncpg.InterfaceError("cannot perform operation: connection is closed")
    assert connection_lost(error, closed=True)
    assert not connection_lost(error, closed=False)
    assert not connection_lost(asyncpg.UniqueViolationError("duplicate key"), closed=True)


# -- the process --------------------------------------------------------------------------------


@pytest.fixture
def world(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
) -> World:
    return World(served, network, editor, db_engine, conn)


@pytest.fixture
async def other(db_url: str) -> AsyncIterator[asyncpg.Connection]:
    """A second connection: it terminates the worker's backend."""
    connection = await asyncpg.connect(asyncpg_dsn(db_url))
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
def logged() -> Iterator[Logged]:
    """Capture the JSON log lines; call the result for the events logged so far."""
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    stream = io.StringIO()
    configure_logging("info", stream=stream)
    try:
        yield lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line]
    finally:
        root.handlers, root.level = saved


def named(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("event") == name]


async def run_until_it_exits(db_url: str, client: RecordingClient) -> int:
    """The long-running worker (no ``--once``): only an error ends it."""
    settings = worker_settings(db_url)
    return await asyncio.wait_for(worker.run(settings, client=client, stop=asyncio.Event()), 30)


async def terminate(other: asyncpg.Connection, conn: asyncpg.Connection) -> None:
    """End ``conn``'s session from the server side and wait until its backend is gone."""
    assert await other.fetchval("SELECT pg_terminate_backend($1, 5000)", conn.get_server_pid())


async def assert_untouched(conn: asyncpg.Connection, ids: list[int]) -> None:
    """Every event still pending with no attempt counted; no watermark."""
    rows = await all_events(conn)
    assert [row["id"] for row in rows] == ids
    assert {(row["status"], row["attempts"], row["last_error"]) for row in rows} == {
        ("pending", 0, None)
    }
    assert await watermark(conn) is None


async def assert_restart_applies_everything_once(
    world: World, db_url: str, client: RecordingClient, records: list[Any], ids: list[int]
) -> None:
    """A restarted worker sends the batch again; QLever ends exact; each event ``done`` once."""
    sent_before = len(client.sent)
    assert await worker.run(worker_settings(db_url), once=True, client=client) == 0
    assert len(client.sent) == sent_before + 1
    rows = await all_events(world.conn)
    assert [(row["status"], row["attempts"]) for row in rows] == [("done", 0)] * len(ids)
    assert all(row["processed_at"] is not None for row in rows)
    assert await watermark(world.conn) == ("number", str(max(ids)))
    assert triples_of(client.store) == await world.library(*records).expected()
    again = await process_batch(world.conn, client)
    assert (again.outcome, again.updates_sent) == ("empty", 0)
    assert len(client.sent) == sent_before + 1


@pytest.mark.parametrize("make", [None, *SESSION_ENDED[:5]], ids=["real", *SESSION_ENDED_IDS[:5]])
async def test_a_server_shutdown_during_the_claim_exits_as_a_lost_connection(
    world: World,
    db_url: str,
    other: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    logged: Logged,
    make: Callable[[], BaseException] | None,
) -> None:
    records, ids = await five_events(world)
    claim = worker._pending_events
    claims: list[int] = []

    async def shut_down(conn: asyncpg.Connection, limit: int) -> list[Any]:
        claims.append(limit)
        if make is not None:
            raise make()
        await terminate(other, conn)
        return await claim(conn, limit)

    monkeypatch.setattr(worker, "_pending_events", shut_down)
    client = RecordingClient()

    assert await run_until_it_exits(db_url, client) == EXIT_FAILURE

    assert claims == [BATCH_SIZE]  # one claim, then the process ends: no retry in-process
    events = logged()
    assert len(named(events, "sync_connection_lost")) == 1
    assert named(events, "sync_worker_failed") == []
    assert named(events, "sync_worker_stopped")
    assert client.sent == []
    await assert_untouched(world.conn, ids)
    monkeypatch.setattr(worker, "_pending_events", claim)
    await assert_restart_applies_everything_once(world, db_url, client, records, ids)


def failing_commit(
    other: asyncpg.Connection, make: Callable[[], BaseException] | None
) -> type[asyncpg.Connection]:
    """A connection class whose first ``COMMIT`` fails: ``make()`` is raised, or (``None``) the
    backend is terminated just before the ``COMMIT`` reaches it."""

    class CommitFails(asyncpg.Connection):  # type: ignore[misc]
        armed = True

        async def execute(self, query: str, *args: Any, **kwargs: Any) -> str:
            if query == "COMMIT;" and CommitFails.armed:
                CommitFails.armed = False
                if make is not None:
                    raise make()
                await terminate(other, self)
            status: str = await super().execute(query, *args, **kwargs)
            return status

    return CommitFails


def connecting_with(
    monkeypatch: pytest.MonkeyPatch, connection_class: type[asyncpg.Connection]
) -> None:
    real_connect = asyncpg.connect

    def connect(*args: Any, **kwargs: Any) -> Awaitable[asyncpg.Connection]:
        return real_connect(*args, connection_class=connection_class, **kwargs)

    monkeypatch.setattr(asyncpg, "connect", connect)


@pytest.mark.parametrize(
    "make",
    [
        None,
        SESSION_ENDED[0],  # admin shutdown
        SESSION_ENDED[1],  # crash shutdown
        SESSION_ENDED[7],  # connection does not exist
        SESSION_ENDED[9],  # transaction resolution unknown
    ],
    ids=["real", "admin_shutdown", "crash_shutdown", "connection_does_not_exist", "unknown"],
)
async def test_a_commit_that_fails_after_qlever_applied_leaves_the_batch_to_be_sent_again(
    world: World,
    db_url: str,
    other: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    logged: Logged,
    make: Callable[[], BaseException] | None,
) -> None:
    records, ids = await five_events(world)
    connecting_with(monkeypatch, failing_commit(other, make))
    client = RecordingClient()

    assert await run_until_it_exits(db_url, client) == EXIT_FAILURE

    assert len(client.sent) == 1  # QLever applied the batch; recording it did not commit
    events = logged()
    assert len(named(events, "sync_connection_lost")) == 1
    assert named(events, "sync_worker_failed") == []
    assert named(events, "sync_batch_done") == []
    await assert_untouched(world.conn, ids)  # retried, not marked done
    monkeypatch.undo()
    await assert_restart_applies_everything_once(world, db_url, client, records, ids)


async def _select_a_missing_column(conn: asyncpg.Connection, limit: int) -> list[Any]:
    return list(await conn.fetch("SELECT no_such_column FROM outbox_events LIMIT $1", limit))


async def _pass_a_wrong_argument_type(conn: asyncpg.Connection, limit: int) -> list[Any]:
    return list(await conn.fetch("SELECT id FROM outbox_events LIMIT $1", f"{limit} rows"))


async def _raise_query_canceled(conn: asyncpg.Connection, limit: int) -> list[Any]:
    raise asyncpg.QueryCanceledError("canceling statement due to statement timeout")


@pytest.mark.parametrize(
    ("where", "error"),
    [
        ("claim", _select_a_missing_column),
        ("claim", _pass_a_wrong_argument_type),
        ("claim", _raise_query_canceled),
        ("commit", None),
    ],
    ids=["undefined_column", "client_data_error", "query_canceled", "unique_violation"],
)
async def test_a_non_retryable_database_error_still_exits_and_is_not_a_lost_connection(
    world: World,
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    logged: Logged,
    where: str,
    error: Callable[[asyncpg.Connection, int], Awaitable[list[Any]]] | None,
) -> None:
    records, ids = await five_events(world)
    calls: list[str] = []
    if where == "claim":
        assert error is not None

        async def broken_claim(conn: asyncpg.Connection, limit: int) -> list[Any]:
            calls.append(where)
            return await error(conn, limit)

        monkeypatch.setattr(worker, "_pending_events", broken_claim)
    else:
        write_watermark = worker._write_watermark

        async def violate(conn: asyncpg.Connection, expected: str | None, highest: int) -> str:
            calls.append(where)
            written = await write_watermark(conn, expected, highest)
            # A duplicate primary key, inside the transaction that records the batch.
            await conn.execute("INSERT INTO outbox_events SELECT * FROM outbox_events LIMIT 1")
            return written

        monkeypatch.setattr(worker, "_write_watermark", violate)
    client = RecordingClient()

    assert await run_until_it_exits(db_url, client) == EXIT_FAILURE

    assert calls == [where]  # it exits: the error is not retried in-process
    events = logged()
    assert named(events, "sync_connection_lost") == []
    [failed] = named(events, "sync_worker_failed")
    assert failed["error_type"] and "Traceback" in failed["exception"]
    if where == "commit":
        assert failed["sqlstate"] == "23505" and len(client.sent) == 1
    else:
        assert client.sent == []
    await assert_untouched(world.conn, ids)
    monkeypatch.undo()
    await assert_restart_applies_everything_once(world, db_url, client, records, ids)
