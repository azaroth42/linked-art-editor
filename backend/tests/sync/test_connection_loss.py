"""How a database error ends each ``lae sync-worker`` command (issues #151, #171 and #176).

Contract §3 item 22, §4, and §8.5 (items 39-43, tests 64-70). A lost connection (the server
shut the session down, the socket is gone, a commit failed that way) is logged
``sync_connection_lost`` and the process exits 1 for its supervisor to restart. Any other
database error is not taken for one: it is logged ``sync_worker_failed`` with its traceback, and
the process exits 4 (``EXIT_PERSISTENT``) when its SQLSTATE class means it cannot pass by itself
(42, 23, 22, 0A), else 1. Either way nothing is recorded for the batch in flight, so the
restarted worker sends it again. ``--replay-after`` and ``--failed`` report the same way. Test
68 reads the systemd unit's restart rules. Tests 73-76 (contract §9): a ``LAE_DATABASE_URL`` that
cannot be parsed refuses to start with exit 2 for every command, one that cannot be reached
exits 1; a metrics port that cannot be bound exits 2, and is bound only after the lock. Tests
77-79 (contract §10, issue #178): every failure of ``asyncpg.connect`` on a URL that parsed is
``sync_database_unreachable`` and exit 1, whatever class it has.

Errors are injected two ways: by raising the error class (every class the worker must recognise,
whether or not this machine's Postgres produces it here), and for real, by terminating the
worker's backend from a second connection with ``pg_terminate_backend``, by selecting a column
that does not exist, or by a statement timeout.
"""

from __future__ import annotations

import asyncio
import configparser
import json
import socket
import ssl
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import asyncpg
import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from lae.bootstrap.common import asyncpg_dsn
from lae.sync import worker
from lae.sync.worker import (
    BATCH_SIZE,
    EXIT_FAILURE,
    EXIT_PERSISTENT,
    ConnectionLostError,
    connection_lost,
    database_failure,
    process_batch,
)
from tests.publish.conftest import Caller
from tests.sync.conftest import (
    TOKEN,
    Logged,
    Network,
    RecordingClient,
    Served,
    all_events,
    insert_event,
    named,
    snapshot_tables,
    triples_of,
    watermark,
)
from tests.sync.test_batch import World, five_events, free_port, metrics_when, worker_settings

pytestmark = pytest.mark.usefixtures("queue")

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


# -- 64-70: the kind of a database error, the exit code, the unit, the commands (contract §8.5) --


async def _select_a_missing_column(conn: asyncpg.Connection, limit: int) -> list[Any]:
    return list(await conn.fetch("SELECT no_such_column FROM outbox_events LIMIT $1", limit))


async def _pass_a_wrong_argument_type(conn: asyncpg.Connection, limit: int) -> list[Any]:
    return list(await conn.fetch("SELECT id FROM outbox_events LIMIT $1", f"{limit} rows"))


async def _raise_query_canceled(conn: asyncpg.Connection, limit: int) -> list[Any]:
    raise asyncpg.QueryCanceledError("canceling statement due to statement timeout")


async def _raise_interface_error(conn: asyncpg.Connection, limit: int) -> list[Any]:
    raise asyncpg.InterfaceError("cannot perform operation: another operation is in progress")


def _server_error(sqlstate: str, severity: str = "ERROR") -> asyncpg.PostgresError:
    error: asyncpg.PostgresError = asyncpg.PostgresError.new(
        {"C": sqlstate, "S": severity, "V": severity, "M": "x"}
    )
    return error


PERSISTENT_SQLSTATES = ["42703", "42P01", "42601", "42501", "23505", "23503", "22P02", "0A000"]
OTHER_SQLSTATES = ["57014", "40001", "40P01", "25006", "53100", "53300", "55P03", "58030", "XX000"]


@pytest.mark.parametrize("make", SESSION_ENDED, ids=SESSION_ENDED_IDS)
def test_database_failure_tells_the_three_kinds_apart(make: Callable[[], BaseException]) -> None:
    """Test 64 (pure): the lost connections of #151, then the persistent and the other kinds."""
    assert database_failure(make()) == "connection_lost"
    assert database_failure(make(), closed=True) == "connection_lost"
    closed = asyncpg.InterfaceError("cannot perform operation: connection is closed")
    assert database_failure(closed, closed=True) == "connection_lost"
    for sqlstate in PERSISTENT_SQLSTATES:
        assert database_failure(_server_error(sqlstate)) == "persistent", sqlstate
    for sqlstate in OTHER_SQLSTATES:
        assert database_failure(_server_error(sqlstate)) == "other", sqlstate
    assert database_failure(asyncpg.PostgresError("no sqlstate")) == "other"
    open_error = asyncpg.InterfaceError("cannot perform operation: another operation")
    assert database_failure(open_error, closed=False) == "other"
    assert database_failure(asyncpg.exceptions._base.DataError("bad argument")) == "other"
    # A FATAL error ends the session whatever its class: a lost connection, never exit 4.
    assert database_failure(_server_error("42703", "FATAL")) == "connection_lost"
    assert database_failure(_server_error("23505", "PANIC")) == "connection_lost"
    assert {c[:2] for c in PERSISTENT_SQLSTATES} == set(worker.PERSISTENT_SQLSTATE_CLASSES)


def _break_the_claim(
    monkeypatch: pytest.MonkeyPatch,
    error: Callable[[asyncpg.Connection, int], Awaitable[list[Any]]],
    calls: list[str],
) -> None:
    async def broken_claim(conn: asyncpg.Connection, limit: int) -> list[Any]:
        calls.append("claim")
        return await error(conn, limit)

    monkeypatch.setattr(worker, "_pending_events", broken_claim)


def _violate_inside_the_recording(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    write_watermark = worker._write_watermark

    async def violate(conn: asyncpg.Connection, expected: str | None, highest: int) -> str:
        calls.append("commit")
        written = await write_watermark(conn, expected, highest)
        # A duplicate primary key, inside the transaction that records the batch.
        await conn.execute("INSERT INTO outbox_events SELECT * FROM outbox_events LIMIT 1")
        return written

    monkeypatch.setattr(worker, "_write_watermark", violate)


@pytest.mark.parametrize(
    ("where", "error", "sqlstate"),
    [
        ("claim", _select_a_missing_column, "42703"),
        ("claim", _pass_a_wrong_argument_type, "22000"),
        ("commit", None, "23505"),
    ],
    ids=["undefined_column", "client_data_error", "unique_violation"],
)
async def test_a_persistent_database_error_exits_4_and_is_not_retried(
    world: World,
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    logged: Logged,
    where: str,
    error: Callable[[asyncpg.Connection, int], Awaitable[list[Any]]] | None,
    sqlstate: str,
) -> None:
    """Test 65. ``client_data_error`` moved here from test 66: asyncpg 0.32 raises a wrong
    argument type as ``asyncpg.exceptions.DataError``, a server-side class with SQLSTATE 22000,
    so ``database_failure`` calls it persistent (a bug in our call; it never passes)."""
    records, ids = await five_events(world)
    calls: list[str] = []
    if error is not None:
        _break_the_claim(monkeypatch, error, calls)
    else:
        _violate_inside_the_recording(monkeypatch, calls)
    client = RecordingClient()

    assert await run_until_it_exits(db_url, client) == EXIT_PERSISTENT == 4

    assert calls == [where]  # the failing call was made once: not retried in-process
    events = logged()
    assert named(events, "sync_connection_lost") == []
    [failed] = named(events, "sync_worker_failed")
    assert (failed["persistent"], failed["exit_code"], failed["command"]) == (True, 4, "run")
    assert failed["sqlstate"] == sqlstate and failed["level"] == "error"
    assert failed["error_type"] and "Traceback" in failed["exception"]
    assert len(client.sent) == (1 if where == "commit" else 0)
    await assert_untouched(world.conn, ids)

    calls.clear()
    once = await worker.run(worker_settings(db_url), once=True, client=client)
    assert once == EXIT_PERSISTENT and calls == [where]
    assert named(logged(), "sync_worker_failed")[-1]["command"] == "once"
    await assert_untouched(world.conn, ids)
    monkeypatch.undo()
    await assert_restart_applies_everything_once(world, db_url, client, records, ids)


@pytest.mark.parametrize(
    "error",
    [_raise_query_canceled, _raise_interface_error],
    ids=["query_canceled", "interface_error_on_an_open_connection"],
)
async def test_a_non_retryable_database_error_still_exits_and_is_not_a_lost_connection(
    world: World,
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    logged: Logged,
    error: Callable[[asyncpg.Connection, int], Awaitable[list[Any]]],
) -> None:
    """Test 66 (amended, contract §8.9): an error of kind ``"other"`` exits 1 and is restarted.

    ``undefined_column`` and ``unique_violation`` moved to test 65, and so did
    ``client_data_error`` (it is SQLSTATE 22000 under asyncpg 0.32, see test 65); a client-side
    ``InterfaceError`` on an open connection takes its place here.
    """
    records, ids = await five_events(world)
    calls: list[str] = []
    _break_the_claim(monkeypatch, error, calls)
    client = RecordingClient()

    assert await run_until_it_exits(db_url, client) == EXIT_FAILURE

    assert calls == ["claim"]  # it exits: the error is not retried in-process
    events = logged()
    assert named(events, "sync_connection_lost") == []
    [failed] = named(events, "sync_worker_failed")
    assert failed["error_type"] and "Traceback" in failed["exception"]
    assert (failed["persistent"], failed["exit_code"]) == (False, EXIT_FAILURE)
    assert client.sent == []
    await assert_untouched(world.conn, ids)
    monkeypatch.undo()
    await assert_restart_applies_everything_once(world, db_url, client, records, ids)


@pytest.mark.parametrize(
    ("where", "make"),
    [
        ("claim", None),
        ("claim", SESSION_ENDED[0]),
        ("claim", SESSION_ENDED[7]),
        ("claim", lambda: _server_error("42P01", "FATAL")),
        ("commit", None),
        ("commit", SESSION_ENDED[1]),
        ("commit", SESSION_ENDED[9]),
    ],
    ids=[
        "claim_terminated",
        "claim_admin_shutdown",
        "claim_connection_does_not_exist",
        "claim_fatal_in_class_42",
        "commit_terminated",
        "commit_crash_shutdown",
        "commit_resolution_unknown",
    ],
)
async def test_a_lost_connection_never_exits_4(
    world: World,
    db_url: str,
    other: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    logged: Logged,
    where: str,
    make: Callable[[], BaseException] | None,
) -> None:
    """Test 67: every way the #151 tests end the session exits 1, never ``EXIT_PERSISTENT``."""
    await five_events(world)
    if where == "claim":
        claim = worker._pending_events

        async def shut_down(conn: asyncpg.Connection, limit: int) -> list[Any]:
            if make is not None:
                raise make()
            await terminate(other, conn)
            return await claim(conn, limit)

        monkeypatch.setattr(worker, "_pending_events", shut_down)
    else:
        connecting_with(monkeypatch, failing_commit(other, make))

    code = await run_until_it_exits(db_url, RecordingClient())

    assert code == EXIT_FAILURE and code != EXIT_PERSISTENT
    events = logged()
    [lost] = named(events, "sync_connection_lost")
    assert lost["command"] == "run"
    assert named(events, "sync_worker_failed") == []


UNIT = Path(__file__).resolve().parents[3] / "deploy" / "systemd" / "lae-sync-worker.service"


def test_the_unit_restarts_after_exit_1_without_limit_and_never_after_2_or_4() -> None:
    """Test 68: the restart rules of ``deploy/systemd/lae-sync-worker.service`` (item 41)."""
    from lae.cli import EXIT_CONFIG_ERROR as CLI_CONFIG_ERROR
    from lae.cli import EXIT_NOT_IMPLEMENTED

    unit = configparser.ConfigParser(strict=True, interpolation=None)
    unit.optionxform = str  # type: ignore[assignment,method-assign]
    unit.read_string(UNIT.read_text())
    assert unit["Unit"]["StartLimitIntervalSec"] == "0"
    assert all("StartLimitBurst" not in unit[section] for section in unit.sections())
    service = unit["Service"]
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "5"
    prevented = {int(code) for code in service["RestartPreventExitStatus"].split()}
    assert prevented == {worker.EXIT_CONFIG_ERROR, worker.EXIT_PERSISTENT}
    assert worker.EXIT_FAILURE not in prevented
    assert worker.EXIT_PERSISTENT == 4 and worker.EXIT_PERSISTENT != EXIT_NOT_IMPLEMENTED
    assert worker.EXIT_CONFIG_ERROR == CLI_CONFIG_ERROR


def failing_fetch(marker: str, how: str, other: asyncpg.Connection, *, after: int) -> type[Any]:
    """A connection class whose ``fetch`` of a query containing ``marker`` fails after ``after``
    successful ones: the session is ``terminated``, a column is undefined, or the statement is
    cancelled by a real ``statement_timeout`` (SQLSTATE 57014)."""

    class FetchFails(asyncpg.Connection):  # type: ignore[misc]
        seen = 0

        async def fetch(self, query: str, *args: Any, **kwargs: Any) -> list[Any]:
            if marker in query:
                FetchFails.seen += 1
                if FetchFails.seen > after:
                    if how == "terminated":
                        await terminate(other, self)
                    elif how == "undefined_column":
                        query, args = "SELECT no_such_column FROM outbox_events", ()
                    else:
                        await super().execute("SET statement_timeout = 1")
                        query, args = "SELECT pg_sleep(1)", ()
            rows: list[Any] = await super().fetch(query, *args, **kwargs)
            return rows

    return FetchFails


@pytest.mark.parametrize(
    ("how", "code", "line", "persistent"),
    [
        ("terminated", EXIT_FAILURE, "sync_connection_lost", None),
        ("undefined_column", EXIT_PERSISTENT, "sync_worker_failed", True),
        ("query_canceled", EXIT_FAILURE, "sync_worker_failed", False),
    ],
)
async def test_replay_after_reports_a_database_error_like_run(
    world: World,
    db_url: str,
    other: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    logged: Logged,
    capsys: pytest.CaptureFixture[str],
    how: str,
    code: int,
    line: str,
    persistent: bool | None,
) -> None:
    """Test 69: the error strikes after one batch was acknowledged; nothing is written."""
    _, ids = await five_events(world)
    assert (await process_batch(world.conn, RecordingClient())).outcome == "done"
    before = await snapshot_tables(world.conn)
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(parse_qs(request.content.decode())["update"][0])
        return httpx.Response(200, json={"status": "OK"})

    real_client = httpx.AsyncClient

    def with_transport(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", with_transport)
    connecting_with(monkeypatch, failing_fetch("WHERE id > $1", how, other, after=1))
    settings = worker_settings(
        db_url, qlever_url="http://qlever.test:7024", qlever_access_token=SecretStr(TOKEN)
    )
    capsys.readouterr()

    exit_code = await worker.main(settings, ["--replay-after", str(ids[0] - 1)])

    out = capsys.readouterr()
    assert exit_code == code
    assert len(sent) == 1  # one batch acknowledged before the error
    assert out.out.splitlines() == [str(ids[-1])]  # the last id acknowledged, exactly once
    assert "replay stopped" in out.err
    events = logged()
    [reported] = named(events, line)
    assert reported["command"] == "replay"
    if persistent is None:
        assert named(events, "sync_worker_failed") == []
    else:
        assert reported["persistent"] is persistent
        assert reported["exit_code"] == code and "Traceback" in reported["exception"]
    monkeypatch.undo()
    assert await snapshot_tables(world.conn) == before
    assert TOKEN not in out.out + out.err + json.dumps(events)


@pytest.mark.parametrize(
    ("how", "code", "line"),
    [
        ("undefined_column", EXIT_PERSISTENT, "sync_worker_failed"),
        ("terminated", EXIT_FAILURE, "sync_connection_lost"),
    ],
)
async def test_failed_reports_a_database_error_like_run(
    world: World,
    db_url: str,
    other: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    logged: Logged,
    capsys: pytest.CaptureFixture[str],
    how: str,
    code: int,
    line: str,
) -> None:
    """Test 70 (scaffolding): ``--failed`` prints no row when its query fails."""
    first = await world.new("Failed one")
    await insert_event(world.conn, first, "publish", 1, 1, status="failed")
    connecting_with(monkeypatch, failing_fetch("WHERE status = 'failed'", how, other, after=0))
    capsys.readouterr()

    assert await worker.main(worker_settings(db_url), ["--failed"]) == code

    assert capsys.readouterr().out == ""
    [reported] = named(logged(), line)
    assert reported["command"] == "failed"
    if line == "sync_worker_failed":
        assert (reported["persistent"], reported["exit_code"]) == (True, EXIT_PERSISTENT)


async def test_replay_after_prints_no_id_when_the_lock_query_fails(
    world: World,
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    logged: Logged,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test 69, continued (added in review of #174): the last id is printed once the command
    holds the lock (item 43). A database error before that is reported the same way, with the
    stderr line, and nothing on stdout: nothing was applied, so there is no id to restart from."""
    await five_events(world)
    before = await snapshot_tables(world.conn)

    class LockFails(asyncpg.Connection):  # type: ignore[misc]
        async def fetchval(self, query: str, *args: Any, **kwargs: Any) -> Any:
            if "pg_try_advisory_lock" in query:
                query, args = "SELECT no_such_column FROM outbox_events", ()
            return await super().fetchval(query, *args, **kwargs)

    connecting_with(monkeypatch, LockFails)
    settings = worker_settings(
        db_url, qlever_url="http://qlever.test:7024", qlever_access_token=SecretStr(TOKEN)
    )
    capsys.readouterr()

    assert await worker.main(settings, ["--replay-after", "0"]) == EXIT_PERSISTENT

    out = capsys.readouterr()
    assert out.out == "" and "replay stopped" in out.err
    [reported] = named(logged(), "sync_worker_failed")
    assert (reported["command"], reported["persistent"]) == ("replay", True)
    monkeypatch.undo()
    assert await snapshot_tables(world.conn) == before


# -- 73-76: a malformed LAE_DATABASE_URL, a metrics port that cannot be bound (contract §9) ------

PASSWORD = "S3cret-pw-9d27"
"""The password in the URLs of tests 73 and 74: never in stderr, stdout or a log line."""

REFUSED_URL = "lae sync-worker: refusing to start: LAE_DATABASE_URL is not a valid PostgreSQL URL: "

COMMANDS: list[list[str]] = [[], ["--once"], ["--replay-after", "0"], ["--failed"]]
COMMAND_IDS = ["run", "once", "replay", "failed"]

MALFORMED_URLS = [
    "nonsense",
    f"lae:{PASSWORD}@localhost/lae",  # no scheme: SQLAlchemy cannot parse it
    f"postgresql+asyncpg://lae:{PASSWORD}@localhost:notaport/lae",  # a port that is no number
    f"postgresql+asyncpg://lae:{PASSWORD}@localhost:5432/lae?sslmode=bogus",  # asyncpg refuses
]
MALFORMED_IDS = ["nonsense", "no_scheme", "port_not_a_number", "asyncpg_client_configuration"]


def command_settings(database_url: str) -> Any:
    """Settings every command accepts (QLever set), with ``database_url``."""
    return worker_settings(
        database_url, qlever_url="http://qlever.test:7024", qlever_access_token=SecretStr(TOKEN)
    )


def assert_no_secret(out: str, err: str, events: list[dict[str, Any]]) -> None:
    for text in (out, err, json.dumps(events)):
        assert PASSWORD not in text and TOKEN not in text
        assert "Traceback" not in text


@pytest.mark.parametrize("argv", COMMANDS, ids=COMMAND_IDS)
@pytest.mark.parametrize("database_url", MALFORMED_URLS, ids=MALFORMED_IDS)
async def test_a_malformed_database_url_refuses_to_start_with_exit_2(
    logged: Logged, capsys: pytest.CaptureFixture[str], argv: list[str], database_url: str
) -> None:
    """Test 73 (§9 item 46): every command; one stderr line, no traceback, no password."""
    capsys.readouterr()

    assert await worker.main(command_settings(database_url), argv) == worker.EXIT_CONFIG_ERROR

    out = capsys.readouterr()
    [line] = out.err.splitlines()
    assert line.startswith(REFUSED_URL) and len(line) > len(REFUSED_URL)
    assert out.out == ""
    events = logged()
    assert named(events, "sync_database_unreachable") == []
    assert named(events, "sync_worker_started") == []
    assert_no_secret(out.out, out.err, events)


@pytest.mark.parametrize("argv", COMMANDS, ids=COMMAND_IDS)
async def test_a_url_error_that_echoes_the_password_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    logged: Logged,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """Test 73, continued: should asyncpg's parse error ever quote the password, as written or
    percent-decoded, the stderr line holds neither."""
    written = "pw%40" + PASSWORD
    url = f"postgresql+asyncpg://lae:{written}@localhost:5432/lae"

    def connect(*args: Any, **kwargs: Any) -> Any:
        raise asyncpg.ClientConfigurationError(f"bad {written} and pw@{PASSWORD}")

    monkeypatch.setattr(asyncpg, "connect", connect)
    capsys.readouterr()

    assert await worker.main(command_settings(url), argv) == worker.EXIT_CONFIG_ERROR

    out = capsys.readouterr()
    assert out.err.splitlines() == [f"{REFUSED_URL}bad [redacted] and [redacted]"]
    assert_no_secret(out.out, out.err, logged())


@pytest.mark.parametrize("argv", COMMANDS, ids=COMMAND_IDS)
async def test_an_unreachable_database_is_still_exit_1(
    logged: Logged, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """Test 74 (§9, item 39 as amended): a URL that parses but whose server cannot be reached is
    ``sync_database_unreachable`` and exit 1 (restarted), never the exit 2 of a malformed URL."""
    port = free_port()  # nothing listens there: the connection is refused
    url = f"postgresql+asyncpg://lae:{PASSWORD}@127.0.0.1:{port}/lae"
    command = COMMAND_IDS[COMMANDS.index(argv)]
    capsys.readouterr()

    assert await worker.main(command_settings(url), argv) == EXIT_FAILURE

    out = capsys.readouterr()
    events = logged()
    [unreachable] = named(events, "sync_database_unreachable")
    assert unreachable["command"] == command and unreachable["level"] == "error"
    assert "refusing to start" not in out.err
    assert ("database unreachable" in out.err) == (command in ("replay", "failed"))
    assert out.out == ""
    assert_no_secret(out.out, out.err, events)


async def test_a_metrics_port_in_use_refuses_to_start_with_exit_2(
    world: World,
    db_url: str,
    other: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    logged: Logged,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test 75 (§9 item 47): the port is bound by the test. Exit 2 with the stderr line, not a
    lost connection; the connection is closed and the lock released; nothing was sent."""
    _, ids = await five_events(world)
    opened: list[asyncpg.Connection] = []

    class Recorded(asyncpg.Connection):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            opened.append(self)

    connecting_with(monkeypatch, Recorded)
    client = RecordingClient()
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        settings = worker_settings(db_url, sync_metrics_port=port)
        capsys.readouterr()

        code = await asyncio.wait_for(worker.run(settings, client=client, stop=asyncio.Event()), 30)

    assert code == worker.EXIT_CONFIG_ERROR == 2
    [line] = capsys.readouterr().err.splitlines()
    prefix = f"lae sync-worker: refusing to start: cannot listen for metrics on 127.0.0.1:{port}: "
    assert line.startswith(prefix) and len(line) > len(prefix)
    events = logged()
    assert named(events, "sync_connection_lost") == []
    assert named(events, "sync_worker_failed") == []
    [conn] = opened
    assert conn.is_closed()
    assert await other.fetchval("SELECT pg_try_advisory_lock($1)", worker.SYNC_WORKER_LOCK)
    await other.execute("SELECT pg_advisory_unlock($1)", worker.SYNC_WORKER_LOCK)
    assert client.sent == []  # it refused before the first catch-up
    await assert_untouched(world.conn, ids)


async def test_the_metrics_port_is_bound_only_after_the_lock(
    world: World,
    db_url: str,
    other: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 76 (§9 item 47): a worker waiting for the lock does not hold the metrics port; it
    binds it once it has the lock."""
    waiting = asyncio.Event()

    async def pause(seconds: float, stop: asyncio.Event) -> None:
        waiting.set()
        await asyncio.sleep(0.01)

    monkeypatch.setattr(worker, "pause", pause)
    port = free_port()
    stop = asyncio.Event()
    settings = worker_settings(db_url, sync_metrics_port=port)
    assert await other.fetchval("SELECT pg_try_advisory_lock($1)", worker.SYNC_WORKER_LOCK)
    task = asyncio.create_task(worker.run(settings, client=RecordingClient(), stop=stop))
    try:
        await asyncio.wait_for(waiting.wait(), 10)
        with socket.socket() as probe:  # free while the worker waits for the lock
            probe.bind(("127.0.0.1", port))
        await other.execute("SELECT pg_advisory_unlock($1)", worker.SYNC_WORKER_LOCK)
        served = await metrics_when(port, "outbox_pending", 0, at_most=True)
        assert served["outbox_pending"] == 0
    finally:
        stop.set()
        assert await asyncio.wait_for(task, 10) == 0


# -- 77-79: asyncpg.connect fails on a URL that parsed (contract §10, issue #178) ----------------


def assert_unreachable(
    code: int, out: Any, events: list[dict[str, Any]], command: str, error_type: str
) -> None:
    """Item 48: ``sync_database_unreachable`` and exit 1, as for a refused connection."""
    assert code == EXIT_FAILURE
    [unreachable] = named(events, "sync_database_unreachable")
    assert unreachable["command"] == command and unreachable["level"] == "error"
    assert unreachable["error_type"] == error_type
    assert "refusing to start" not in out.err
    assert ("database unreachable" in out.err) == (command in ("replay", "failed"))
    assert out.out == ""
    assert named(events, "sync_worker_started") == []
    assert_no_secret(out.out, out.err, events)


@pytest.mark.parametrize("argv", COMMANDS, ids=COMMAND_IDS)
async def test_a_failed_tls_handshake_is_an_unreachable_database_not_a_malformed_url(
    monkeypatch: pytest.MonkeyPatch,
    logged: Logged,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """Test 77 (§10 item 48). ``ssl.SSLCertVerificationError`` is an ``OSError`` and a
    ``ValueError``; §9 took every ``ValueError`` of ``asyncpg.connect`` for a parse error, so an
    expired server certificate exited 2 and the unit never started the worker again."""
    assert issubclass(ssl.SSLCertVerificationError, ValueError)
    assert issubclass(ssl.SSLCertVerificationError, OSError)

    async def connect(*args: Any, **kwargs: Any) -> Any:
        raise ssl.SSLCertVerificationError(1, "certificate verify failed: certificate has expired")

    monkeypatch.setattr(asyncpg, "connect", connect)
    url = f"postgresql+asyncpg://lae:{PASSWORD}@db.example:5432/lae?ssl=verify-full"
    capsys.readouterr()

    code = await worker.main(command_settings(url), argv)

    assert_unreachable(
        code,
        capsys.readouterr(),
        logged(),
        COMMAND_IDS[COMMANDS.index(argv)],
        "SSLCertVerificationError",
    )


AUTH_SCM_CREDENTIAL = 6  # an authentication request asyncpg does not implement


async def _ask_for_an_unsupported_login(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """The start of the protocol: no TLS, then ``AuthenticationSCMCredential``."""
    try:
        (length,) = struct.unpack("!i", await reader.readexactly(4))
        body = await reader.readexactly(length - 4)
        if body == struct.pack("!i", 80877103):  # SSLRequest
            writer.write(b"N")
            (length,) = struct.unpack("!i", await reader.readexactly(4))
            await reader.readexactly(length - 4)  # the startup message
        writer.write(b"R" + struct.pack("!ii", 8, AUTH_SCM_CREDENTIAL))
        await writer.drain()
        await reader.read()  # until the client hangs up
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


@pytest.mark.parametrize("argv", COMMANDS, ids=COMMAND_IDS)
async def test_an_interface_error_after_the_socket_opened_is_an_unreachable_database(
    logged: Logged, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """Test 78 (§10 item 48; the gap §9.7 recorded). A real socket: the server answers the
    startup message with an authentication request asyncpg does not implement, which asyncpg
    raises as a plain ``InterfaceError``. It left ``main`` as a traceback."""
    server = await asyncio.start_server(_ask_for_an_unsupported_login, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"postgresql+asyncpg://lae:{PASSWORD}@127.0.0.1:{port}/lae"
    capsys.readouterr()
    try:
        code = await asyncio.wait_for(worker.main(command_settings(url), argv), 30)
    finally:
        server.close()
        await server.wait_closed()

    events = logged()
    assert_unreachable(
        code, capsys.readouterr(), events, COMMAND_IDS[COMMANDS.index(argv)], "InterfaceError"
    )
    [unreachable] = named(events, "sync_database_unreachable")
    assert "unsupported authentication method" in unreachable["error"]


@pytest.mark.parametrize("argv", COMMANDS, ids=COMMAND_IDS)
async def test_no_host_with_the_session_attributes_asked_for_is_an_unreachable_database(
    db_url: str, logged: Logged, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """Test 79 (§10 item 48). The test database is a primary; a URL that asks for a standby
    connects, finds none, and asyncpg raises ``TargetServerAttributeNotMatched`` (an
    ``InternalClientError``, neither ``InterfaceError`` nor ``PostgresError``). After a failover
    the same URL connects, so it is exit 1, restarted."""
    url = db_url + ("&" if "?" in db_url else "?") + "target_session_attrs=standby"
    capsys.readouterr()

    code = await asyncio.wait_for(worker.main(command_settings(url), argv), 30)

    assert_unreachable(
        code,
        capsys.readouterr(),
        logged(),
        COMMAND_IDS[COMMANDS.index(argv)],
        "TargetServerAttributeNotMatched",
    )
