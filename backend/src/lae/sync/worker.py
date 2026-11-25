"""``lae sync-worker``: apply the outbox to QLever (Phase 1 Step 1.11; spec §3.3, decision A13).

The contract is ``docs/contracts/1.11-sync-worker.md``; ADR-0034 records where it departs from the
guide text. ``lae.sync.events`` turns one event into its SPARQL Update; this module batches,
sends, and records.

**Batches** (``process_batch``). A batch is the pending events with the lowest ids, ascending,
at most ``BATCH_SIZE``. Their updates are joined with ``" ;\\n"`` in id order and sent in one
request; events of one record stay separate updates (no coalescing). The batch is cut before the
first event that would take the text over ``MAX_UPDATE_BYTES`` (one event alone is always sent)
and before the first event whose translation raises. Nothing is written to Postgres between
reading the batch and the transaction that records it.

**Acknowledge, then record.** Only after QLever answers 2xx does one transaction set the events
``done`` (``processed_at = now()``; the ``UPDATE`` must change exactly the batch's rows) and
``sync_state.qlever_index_watermark`` to the batch's highest id (never lower than it was). The
watermark is compared on write: it is read before translating and again ``FOR UPDATE`` in that
transaction; if someone changed it, the transaction is rolled back and the outcome is
``stale_watermark`` (``run`` then catches up). A watermark lowered while no batch was in flight
is noticed too: a ``done`` event above the watermark is one the live index may not hold, so
``process_batch`` sends nothing and answers ``stale_watermark``, and the idle loop makes the same
check on every poll. A crash anywhere leaves each event ``pending``
(its update possibly applied, wholly or in part) or ``done`` with its update applied; sending a
batch again is harmless because applying events in order is idempotent (contract §3 item 17).

**Failures.** ``classify``: no answer, 401, 403, 408, 429 and 5xx are ``unavailable`` and cost
nothing (no write; ``run`` retries after ``backoff_seconds`` of the consecutive outages, capped at
30 s, for as long as it takes). Any other status is ``rejected``: a rejected batch of several
events is sent again one event at a time, each recorded on its own, stopping at the first
failure. An event rejected alone is confirmed before it is blamed (``confirm_rejection``,
contract §8, issue #171): ``PROBE_UPDATE`` (a ``DELETE WHERE`` on a subject nobody owns) must be
acknowledged and the event's update, sent alone again, rejected again. Only then does the event
get ``attempts + 1`` and ``last_error`` (the second rejection's); after ``MAX_ATTEMPTS`` it becomes
``failed`` (a dead letter) and the queue moves on. An event whose translation raises at the head
of a batch is counted the same way, without asking QLever. An event accepted on the second send
is recorded ``done`` like any other. A rejected probe means the endpoint refuses every update (a
wrong update URL answering 404 or 405): the outcome is ``unavailable`` with a ``Refusal``, nothing
is written, and ``run`` logs ``sync_updates_refused`` and backs off as for an outage. The
record's next event after a dead letter replaces the record's triples over the versions the dead
letter left uncertain (``lae.sync.events``).

**Catch-up** (``catch_up``; contract §7, issue #150). When the watermark exists and a ``done``
event has a higher id (an index rebuilt from an older export, a restored index directory), those
events are replayed before any pending event, in batches planned like ``process_batch``'s, and
the watermark is written (compared on write) after each acknowledged batch. Catch-up never stops
on one event: a ``done`` event that cannot be replayed becomes ``failed``, a dead letter like a
poisoned pending event, and its record's next event heals it with a replace. Only a confirmed
permanent failure counts: the event's translation raises at the head of a batch
(``LAE-SYNC-001`` to ``-003``), or its update, sent alone, is rejected, then ``PROBE_UPDATE`` (a
``DELETE WHERE`` on a subject nobody owns) is acknowledged, then the update is rejected again. A
rejected combined update is narrowed by sending its events alone, each recorded on its own. The
confirmation is the live path's ``confirm_rejection``. The dead letter is one transaction that
compares the watermark and sets ``done`` to ``failed`` with ``last_error`` (``"catch_up":
true``); it does not move the watermark or change ``attempts``, and
catch-up then plans again from the watermark, so a later event of the record is translated as a
replace. An unavailable QLever, at any send, writes nothing and leaves ``catch_up``; a rejected
probe means the endpoint refuses every update and raises ``UpdatesRefusedError``, which
``classify`` calls ``unavailable``. ``run`` retries both with backoff (``--once`` exits 1).

**Replay** (``replay``, ``apply_event``). Both take any ``QLeverClient`` as the target and write
nothing to Postgres, so Step 2.18 can replay into a second index; ``replay`` still raises on the
first failure. ``process_batch`` and ``catch_up`` are the only writers of ``outbox_events`` and
``sync_state``. ``lae sync-worker --failed`` lists the dead letters and only reads.

**Process** (``run``). One ``asyncpg`` connection holds ``pg_try_advisory_lock(SYNC_WORKER_LOCK)``,
listens on ``outbox`` and makes every read and write. A notification or the 1 s poll wakes the
loop; with ``BATCH_SIZE`` or more events pending it takes a batch at once, otherwise it first
waits until ``BATCH_WINDOW_S`` after waking. If the connection is lost the process exits 1 and
never reconnects in-process (a supervisor restarts it), so a lost lock is never used. A lost
connection (``connection_lost``) includes the server ending the session (shutdown, terminated
backend, a ``FATAL`` error) and a commit that fails that way; it is logged
``sync_connection_lost``. Any other database error is not taken for one: it is logged
``sync_worker_failed`` with its traceback. ``database_failure`` tells the kinds apart, and
``report_database_error`` logs them and picks the exit code for every command (``run``,
``--once``, ``--replay-after``, ``--failed``; contract §8.5):

- 0: stopped by ``SIGTERM``/``SIGINT``; ``--once`` with ``empty`` or ``done``; a finished
  ``--replay-after``; ``--failed``.
- 1 (``EXIT_FAILURE``): may pass by itself, or unknown: the database unreachable, the connection
  lost, a database error of kind ``"other"`` (a cancelled statement, a deadlock, a full disk, an
  internal error, a client-side ``InterfaceError``), ``RecordingError``, an ``EventError`` or
  ``QLeverError`` that reaches ``run``; ``--once`` or ``--replay-after`` that did not finish.
- 2 (``EXIT_CONFIG_ERROR``): refuses to start (a setting missing, ``LAE_DATABASE_URL`` not a
  valid PostgreSQL URL, the metrics port cannot be bound, the watermark not an integer).
- 4 (``EXIT_PERSISTENT``): a database error that cannot pass by itself, SQLSTATE class 0A, 22,
  23 or 42 (our SQL against the wrong schema, a violated constraint, a missing privilege).

The systemd unit restarts after 1 for ever and never after 2 or 4. Nothing is retried
in-process: the batch in flight stays pending and is sent again after the restart.
``SIGTERM`` and ``SIGINT`` let the batch in progress finish and exit 0. ``/metrics``
(``lae.sync.metrics``) is served from a snapshot the loop refreshes, so the listener needs no
connection of its own; it is started after the lock is taken and before the first catch-up, and
a port it cannot listen on refuses the start (exit 2; contract §9).

The access token goes with every update and is removed from every error text the worker logs or
stores, before the text is cut or encoded as JSON (``redacted_body``; contract §8.6).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
from collections.abc import AsyncGenerator, Collection, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import unquote, urlsplit

import asyncpg
import sqlalchemy.exc

from lae.bootstrap.common import QLEVER_INDEX_WATERMARK, asyncpg_dsn
from lae.db.models import OutboxEventStatus
from lae.logging import get_logger
from lae.settings import Settings
from lae.sparql.updates import build_delete_where_subjects
from lae.sync import metrics as sync_metrics
from lae.sync.errors import REJECTED_BODY_CHARS, EventError, rejected
from lae.sync.events import (
    EVENT_COLUMNS,
    UPDATE_SEPARATOR,
    Event,
    event_from_row,
    translate_event,
)
from lae.sync.qlever_client import QLeverClient, QLeverError

BATCH_SIZE: Final = 50
BATCH_WINDOW_S: Final = 0.25
POLL_INTERVAL_S: Final = 1.0
MAX_ATTEMPTS: Final = 5
MAX_UPDATE_BYTES: Final = 4_000_000
SYNC_WORKER_LOCK: Final = 0x1AE0_51C0  # advisory lock key, one per database

LOCK_RETRY_S: Final = 5.0
"""How often a worker that found the lock held tries again."""

MAX_BACKOFF_S: Final = 30.0

EXIT_FAILURE: Final = 1
"""Exit code of a failure that may pass by itself, or of unknown kind (contract §8.5 item 40).

The database is unreachable, the connection is lost, a database error of kind ``"other"``,
``RecordingError``, an ``EventError`` or ``QLeverError`` that reaches ``run``; ``--once``: the
lock is held or the outcome is not ``empty`` or ``done``; ``--replay-after``: the lock is held or
the replay stopped. The unit restarts the worker after it, for ever.
"""

EXIT_CONFIG_ERROR: Final = 2
"""Exit code of a worker that refuses to start (the same value as ``lae.cli.EXIT_CONFIG_ERROR``).

A setting is missing, ``LAE_DATABASE_URL`` cannot be parsed, the metrics port cannot be bound,
the watermark is not a JSON integer, or the arguments are wrong. The unit does not restart the
worker after it (``RestartPreventExitStatus=``).
"""

EXIT_PERSISTENT: Final = 4
"""Exit code of a database error that cannot pass by itself (``database_failure`` says
``"persistent"``). The unit does not restart the worker after it; 3 is
``lae.cli.EXIT_NOT_IMPLEMENTED``."""

PERSISTENT_SQLSTATE_CLASSES: Final = frozenset({"0A", "22", "23", "42"})
"""SQLSTATE classes that mean "this statement is wrong for this schema or this data".

0A feature not supported; 22 data exception; 23 integrity constraint violation; 42 syntax error
or access rule violation (undefined table or column, insufficient privilege). Every other class
is of unknown persistence and exits 1 (contract §8.1 item 2)."""

OUTBOX_CHANNEL: Final = "outbox"
STOP_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)

_UNAVAILABLE_STATUSES: Final = frozenset({401, 403, 408, 429})
_REDACTED: Final = "[redacted]"

PROBE_SUBJECT: Final = "urn:lae:sync-probe"
PROBE_UPDATE: Final = build_delete_where_subjects((PROBE_SUBJECT,))
"""``DELETE WHERE { <urn:lae:sync-probe> ?p ?o }``: matches nothing, changes nothing.

``confirm_rejection`` sends it after an event was rejected alone, on the live path and in
catch-up: if QLever accepts it, the endpoint takes updates and the event is at fault; if not, the
endpoint refuses every update (contract §7 and §8)."""

logger = get_logger(__name__)

Outcome = Literal["empty", "done", "rejected", "unavailable", "stale_watermark"]
DatabaseFailure = Literal["connection_lost", "persistent", "other"]
Command = Literal["run", "once", "replay", "failed"]


@dataclass(frozen=True, slots=True)
class Refusal:
    """The probe's answer, when QLever rejected the probe (contract §8.3)."""

    status: int
    body: str  # token removed, then the first 2000 characters (item 44)


@dataclass(frozen=True, slots=True)
class BatchResult:
    """What one ``process_batch`` call did."""

    outcome: Outcome
    done: tuple[int, ...]  # ids marked done by this call, ascending
    failed_event: int | None  # the event that got an attempt counted, if any
    dead_lettered: bool  # that event is now `failed`
    updates_sent: int  # HTTP updates QLever acknowledged
    error: str | None
    refusal: Refusal | None = None  # set only with outcome "unavailable": the probe was rejected


class WatermarkError(ValueError):
    """``sync_state.qlever_index_watermark`` exists but is not a JSON integer."""


class RecordingError(RuntimeError):
    """The transaction recording a batch or an attempt did not change the rows it must."""


class ConnectionLostError(RuntimeError):
    """The worker's one Postgres connection is closed; nothing more may be sent."""


class UpdatesRefusedError(QLeverError):
    """QLever rejected the probe: it refuses every update; no event is at fault.

    ``status`` and ``body`` are the probe's answer. ``classify`` calls it ``unavailable``
    whatever its status (contract §3 item 19 as amended, §7.3).
    """


class _StaleWatermark(Exception):
    """The watermark differs from the value read before the batch (rolls the transaction back)."""


# -- classification and backoff -----------------------------------------------------------------


def classify(error: QLeverError) -> Literal["unavailable", "rejected"]:
    """``unavailable`` without a status and for 401, 403, 408, 429 and 5xx; else ``rejected``.

    An ``UpdatesRefusedError`` (the probe was rejected) is ``unavailable`` whatever its status.
    """
    if isinstance(error, UpdatesRefusedError):
        return "unavailable"
    status = error.status
    if status is None or status in _UNAVAILABLE_STATUSES or 500 <= status <= 599:
        return "unavailable"
    return "rejected"


def backoff_seconds(failures: int) -> float:
    """``min(2 ** (failures - 1), 30)``: 1, 2, 4, 8, 16, 30, 30, ... seconds."""
    return float(min(2 ** max(failures - 1, 0), MAX_BACKOFF_S))


_SESSION_ENDED: Final[tuple[type[BaseException], ...]] = (
    ConnectionLostError,
    OSError,  # the socket: reset, refused, timed out
    asyncpg.PostgresConnectionError,  # SQLSTATE class 08, and "closed in the middle of operation"
    asyncpg.AdminShutdownError,  # 57P01: pg_terminate_backend, smart or fast shutdown
    asyncpg.CrashShutdownError,  # 57P02: another backend crashed; the server restarts
    asyncpg.CannotConnectNowError,  # 57P03: the server is starting or stopping
    asyncpg.DatabaseDroppedError,  # 57P04
    asyncpg.IdleSessionTimeoutError,  # 57P05
    asyncpg.IdleInTransactionSessionTimeoutError,  # 25P03
    asyncpg.TransactionTimeoutError,  # 25P04
)
"""Errors that mean the server ended the worker's session or the socket is gone."""

_SESSION_ENDING_SEVERITIES: Final = frozenset({"FATAL", "PANIC"})


def connection_lost(error: BaseException, *, closed: bool = False) -> bool:
    """Whether ``error`` means the worker's connection is gone (contract §3 item 22, §4).

    True for the errors in ``_SESSION_ENDED``, for any server error of severity ``FATAL`` or
    ``PANIC`` (the server ends the session after sending one), and for an ``InterfaceError`` when
    the connection is ``closed`` ("connection is closed"). Everything else is not: a bug in our
    SQL, a constraint violation, a cancelled statement, a serialization failure, or a client-side
    ``InterfaceError`` on an open connection (overlapping operations). A query argument of the
    wrong type is not an ``InterfaceError``: asyncpg raises it as ``DataError``, SQLSTATE 22000
    (contract §8.3). ``database_failure`` tells the rest apart.
    """
    if isinstance(error, _SESSION_ENDED):
        return True
    if isinstance(error, asyncpg.PostgresError):
        return getattr(error, "severity_en", None) in _SESSION_ENDING_SEVERITIES
    return isinstance(error, asyncpg.InterfaceError) and closed


def database_failure(error: BaseException, *, closed: bool = False) -> DatabaseFailure:
    """The kind of a database error the worker exits on (contract §8.3, §8.5 item 39).

    ``"connection_lost"`` when ``connection_lost`` says so (checked first: a ``FATAL`` error is a
    lost connection whatever its class); ``"persistent"`` for an ``asyncpg.PostgresError`` whose
    SQLSTATE is in ``PERSISTENT_SQLSTATE_CLASSES``; ``"other"`` for everything else (another
    SQLSTATE, a ``PostgresError`` without one, a client-side ``InterfaceError`` on an open
    connection).
    """
    if connection_lost(error, closed=closed):
        return "connection_lost"
    if isinstance(error, asyncpg.PostgresError):
        sqlstate = getattr(error, "sqlstate", None)
        if isinstance(sqlstate, str) and sqlstate[:2] in PERSISTENT_SQLSTATE_CLASSES:
            return "persistent"
    return "other"


def _redact_token(text: str, token: str | None) -> str:
    return text.replace(token, _REDACTED) if token else text


def _redact(text: str, client: QLeverClient) -> str:
    return _redact_token(text, client.access_token)


def redacted_body(body: str, client: QLeverClient) -> str:
    """QLever's answer with the access token removed, then cut to ``REJECTED_BODY_CHARS``.

    In that order (contract §8.6 items 44 and 45): a token across the cut is removed whole, and
    the text is redacted before ``EventError`` encodes it as JSON. Every body the worker stores or
    logs goes through here.
    """
    return _redact(body, client)[:REJECTED_BODY_CHARS]


def _rejection(event_id: int, error: QLeverError, client: QLeverClient) -> EventError:
    """``LAE-SYNC-004`` for ``event_id`` with QLever's answer redacted, then cut."""
    return rejected(event_id, error.status, redacted_body(error.body, client))


# -- the watermark ------------------------------------------------------------------------------


async def _read_watermark(conn: asyncpg.Connection, *, for_update: bool = False) -> str | None:
    """The watermark as JSON text, or ``None`` when the key is missing."""
    lock = " FOR UPDATE" if for_update else ""
    raw: str | None = await conn.fetchval(
        f"SELECT value::text FROM sync_state WHERE key = $1{lock}", QLEVER_INDEX_WATERMARK
    )
    return raw


def watermark_value(raw: str | None) -> int | None:
    """The watermark as an integer, ``None`` if missing; ``WatermarkError`` if not an integer."""
    if raw is None:
        return None
    value = json.loads(raw)
    if isinstance(value, bool) or not isinstance(value, int):
        raise WatermarkError(f"sync_state.{QLEVER_INDEX_WATERMARK} is {raw}, not an integer")
    return value


async def _done_above(conn: asyncpg.Connection, watermark: int) -> bool:
    """True if a ``done`` event has an id above ``watermark``: the live index needs a catch-up.

    The worker alone never leaves one (it writes the watermark with the events it marks done), so
    this means someone lowered the watermark: an index rebuilt from an older export, a restored
    index directory (contract §3 items 14 and 15).
    """
    behind: bool = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM outbox_events WHERE status = 'done' AND id > $1)",
        watermark,
    )
    return behind


async def _behind(conn: asyncpg.Connection) -> bool:
    """``_done_above`` the stored watermark; ``False`` without the key (``WatermarkError`` too)."""
    watermark = watermark_value(await _read_watermark(conn))
    return watermark is not None and await _done_above(conn, watermark)


async def _write_watermark(conn: asyncpg.Connection, expected: str | None, highest: int) -> str:
    """Inside a transaction: compare, then set the watermark to ``max(current, highest)``.

    Raises ``_StaleWatermark`` if the value is not ``expected`` (a missing key is a value too).
    Returns the JSON text written.
    """
    current = await _read_watermark(conn, for_update=True)
    if current != expected:
        raise _StaleWatermark
    previous = watermark_value(current)
    value = json.dumps(highest if previous is None else max(previous, highest))
    if current is None:
        status = await conn.execute(
            "INSERT INTO sync_state (key, value) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (key) DO NOTHING",
            QLEVER_INDEX_WATERMARK,
            value,
        )
        if status != "INSERT 0 1":  # inserted by someone else since we looked
            raise _StaleWatermark
    else:
        await conn.execute(
            "UPDATE sync_state SET value = $2::jsonb WHERE key = $1", QLEVER_INDEX_WATERMARK, value
        )
    return value


async def _record_done(conn: asyncpg.Connection, ids: Sequence[int], expected: str | None) -> str:
    """One transaction: mark ``ids`` done and move the watermark; return the watermark written.

    Raises ``_StaleWatermark`` (rolled back) or ``RecordingError`` (rolled back) if an event was
    no longer pending.
    """
    async with conn.transaction():
        written = await _write_watermark(conn, expected, max(ids))
        status = await conn.execute(
            "UPDATE outbox_events SET status = 'done', processed_at = now() "
            "WHERE id = ANY($1::bigint[]) AND status = 'pending'",
            list(ids),
        )
        if int(status.rsplit(" ", 1)[-1]) != len(ids):
            raise RecordingError(f"outbox events {list(ids)} were not all pending: {status}")
    return written


async def _count_attempt(conn: asyncpg.Connection, event: Event, error: str) -> tuple[int, bool]:
    """One transaction: ``attempts + 1`` and ``last_error``; a dead letter at ``MAX_ATTEMPTS``.

    Returns ``(attempts, dead_lettered)``. The watermark is not touched.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            UPDATE outbox_events SET
              attempts = attempts + 1,
              last_error = $2,
              status = CASE WHEN attempts + 1 >= $3
                            THEN 'failed'::outbox_events_status ELSE status END,
              processed_at = CASE WHEN attempts + 1 >= $3 THEN now() ELSE processed_at END
            WHERE id = $1 AND status = 'pending'
            RETURNING attempts, status::text AS status
            """,
            event.id,
            error,
            MAX_ATTEMPTS,
        )
        if row is None:
            raise RecordingError(f"outbox event {event.id} is no longer pending")
    attempts: int = row["attempts"]
    return attempts, row["status"] == OutboxEventStatus.FAILED.value


# -- building and sending batches ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Plan:
    """The events of a batch with their updates, or the translation error of its head event."""

    events: tuple[Event, ...]
    updates: tuple[str, ...]
    head_error: EventError | None = None

    @property
    def text(self) -> str:
        return UPDATE_SEPARATOR.join(update for update in self.updates if update)

    @property
    def ids(self) -> tuple[int, ...]:
        return tuple(event.id for event in self.events)


async def _plan(conn: asyncpg.Connection, events: Sequence[Event]) -> _Plan:
    """Translate ``events`` in order and cut the batch (contract §3 item 11)."""
    included: list[Event] = []
    updates: list[str] = []
    size = 0
    for event in events:
        try:
            update = await translate_event(conn, event)
        except EventError as error:
            if not included:
                return _Plan((event,), ("",), head_error=error)
            break
        added = len(update.encode("utf-8"))
        if added and size:
            added += len(UPDATE_SEPARATOR)
        if included and size + added > MAX_UPDATE_BYTES:
            break
        included.append(event)
        updates.append(update)
        size += added
    return _Plan(tuple(included), tuple(updates))


async def _pending_events(conn: asyncpg.Connection, limit: int) -> list[Event]:
    rows = await conn.fetch(
        f"SELECT {EVENT_COLUMNS} FROM outbox_events WHERE status = 'pending' ORDER BY id LIMIT $1",
        limit,
    )
    return [event_from_row(row) for row in rows]


def _ensure_open(conn: asyncpg.Connection) -> None:
    if conn.is_closed():
        raise ConnectionLostError("the database connection is closed")


async def confirm_rejection(
    conn: asyncpg.Connection, client: QLeverClient, update: str
) -> QLeverError | None:
    """``update``, the update of one event sent alone, was just rejected: confirm it (items 26, 34).

    Sends ``PROBE_UPDATE``, then ``update`` again, with nothing else between, and checks that the
    connection is open before each (``ConnectionLostError`` otherwise). Returns ``None`` when the
    second send is acknowledged, and its ``QLeverError`` when it is rejected again. Raises
    ``UpdatesRefusedError`` (the probe's status and body) when the probe is rejected, and a
    ``QLeverError`` that ``classify`` calls ``unavailable`` unchanged, from either request. Writes
    nothing to Postgres and counts nothing: the live path and catch-up both call it, and differ
    only in what follows a confirmed rejection (contract §8.4 item 35).
    """
    _ensure_open(conn)
    try:
        await client.update(PROBE_UPDATE)
    except QLeverError as error:
        if classify(error) == "unavailable":
            raise
        raise UpdatesRefusedError(
            "QLever refused the probe update", status=error.status, body=error.body
        ) from error
    _ensure_open(conn)
    try:
        await client.update(update)
    except QLeverError as again:
        if classify(again) == "unavailable":
            raise
        return again
    return None


async def _attempt_failed(
    conn: asyncpg.Connection,
    event: Event,
    error: str,
    *,
    done: tuple[int, ...] = (),
    updates_sent: int = 0,
) -> BatchResult:
    attempts, dead = await _count_attempt(conn, event, error)
    log = logger.error if dead else logger.warning
    log(
        "sync_dead_letter" if dead else "sync_event_failed",
        event_id=event.id,
        record_id=str(event.record_id),
        attempts=attempts,
        error=error,
        during="live",
    )
    return BatchResult("rejected", done, event.id, dead, updates_sent, error)


def _unavailable(
    error: QLeverError, client: QLeverClient, done: tuple[int, ...], updates_sent: int
) -> BatchResult:
    """The outcome of an ``unavailable`` error, with a ``Refusal`` when the probe was rejected."""
    refusal = None
    if isinstance(error, UpdatesRefusedError):
        assert error.status is not None  # the probe got an answer: rejected, not unreachable
        refusal = Refusal(error.status, redacted_body(error.body, client))
    text = _redact(str(error), client)
    return BatchResult("unavailable", done, None, False, updates_sent, text, refusal)


async def _rejected_alone(
    conn: asyncpg.Connection,
    client: QLeverClient,
    event: Event,
    update: str,
    *,
    done: tuple[int, ...] = (),
    updates_sent: int = 0,
) -> BatchResult | None:
    """``update`` of ``event``, sent alone, was rejected: confirm it, then count (items 34-37).

    Returns ``None`` when the second send was acknowledged: the caller records the event ``done``
    like any acknowledged event. Otherwise the result: ``unavailable`` (with a ``Refusal`` when
    the probe was rejected) writes nothing; a confirmed rejection counts one attempt with the
    ``LAE-SYNC-004`` of the second send.
    """
    try:
        again = await confirm_rejection(conn, client, update)
    except QLeverError as error:  # unavailable at the probe or the second send, or refused
        return _unavailable(error, client, done, updates_sent)
    if again is None:
        return None
    failure = _redact(str(_rejection(event.id, again, client)), client)
    return await _attempt_failed(conn, event, failure, done=done, updates_sent=updates_sent)


async def _send_one_by_one(
    conn: asyncpg.Connection, client: QLeverClient, plan: _Plan, expected: str | None
) -> BatchResult:
    """After a rejected batch: send its events alone, in order, recording each (item 21).

    The first event rejected alone is confirmed (``_rejected_alone``); if its second send is
    acknowledged, it is recorded like the others and the loop goes on.
    """
    done: list[int] = []
    sent = 0
    for event, update in zip(plan.events, plan.updates, strict=True):
        if update:
            _ensure_open(conn)
            try:
                await client.update(update)
            except QLeverError as error:
                if classify(error) == "unavailable":
                    return _unavailable(error, client, tuple(done), sent)
                failed = await _rejected_alone(
                    conn, client, event, update, done=tuple(done), updates_sent=sent
                )
                if failed is not None:
                    return failed
            sent += 1
        try:
            expected = await _record_done(conn, (event.id,), expected)
        except _StaleWatermark:
            return BatchResult("stale_watermark", tuple(done), None, False, sent, None)
        done.append(event.id)
    return BatchResult("done", tuple(done), None, False, sent, None)


async def process_batch(
    conn: asyncpg.Connection, client: QLeverClient, *, limit: int = BATCH_SIZE
) -> BatchResult:
    """Send the next batch of pending events to the live index and record the outcome.

    When a ``done`` event has an id above the watermark (it was lowered while no batch was in
    flight), nothing is sent or written and the outcome is ``stale_watermark``: the caller must
    ``catch_up`` first, or the batch would move the watermark past events the index never got.

    An event rejected alone is confirmed by ``confirm_rejection`` before an attempt is counted;
    a rejected probe is ``unavailable`` with ``refusal`` set (contract §8.4).

    Raises ``WatermarkError`` (before anything is sent) if the watermark is not an integer,
    ``RecordingError`` if recording found an event no longer pending, ``ConnectionLostError`` if
    the connection closed before sending, and whatever ``asyncpg`` raises when the database is
    gone. The outcomes are described in the module docstring.
    """
    expected = await _read_watermark(conn)
    watermark = watermark_value(expected)
    if watermark is not None and await _done_above(conn, watermark):
        return BatchResult("stale_watermark", (), None, False, 0, None)
    events = await _pending_events(conn, limit)
    if not events:
        return BatchResult("empty", (), None, False, 0, None)
    plan = await _plan(conn, events)
    if plan.head_error is not None:
        return await _attempt_failed(conn, plan.events[0], str(plan.head_error))
    text = plan.text
    sent = 0
    if text:
        _ensure_open(conn)
        try:
            await client.update(text)
        except QLeverError as error:
            if classify(error) == "unavailable":
                return _unavailable(error, client, (), 0)
            if len(plan.events) > 1:
                return await _send_one_by_one(conn, client, plan, expected)
            failed = await _rejected_alone(conn, client, plan.events[0], plan.updates[0])
            if failed is not None:
                return failed
        sent = 1
    try:
        await _record_done(conn, plan.ids, expected)
    except _StaleWatermark:
        return BatchResult("stale_watermark", (), None, False, sent, None)
    return BatchResult("done", plan.ids, None, False, sent, None)


# -- replay -------------------------------------------------------------------------------------


async def apply_event(conn: asyncpg.Connection, event_id: int, client: QLeverClient) -> str:
    """Translate one event (in any status) and send it to ``client``; return the text sent.

    Writes nothing to Postgres. Returns ``""`` when there was nothing to send. For a caller that
    replays in id order: an old event sent on its own can undo a later one (contract item 17).
    Raises ``LookupError`` for an unknown id, ``EventError``, ``QLeverError``.
    """
    row = await conn.fetchrow(f"SELECT {EVENT_COLUMNS} FROM outbox_events WHERE id = $1", event_id)
    if row is None:
        raise LookupError(f"no outbox event {event_id}")
    update = await translate_event(conn, event_from_row(row))
    if update:
        await client.update(update)
    return update


async def replay_batches(
    conn: asyncpg.Connection,
    client: QLeverClient,
    *,
    after_id: int,
    through_id: int | None = None,
    statuses: Collection[OutboxEventStatus] = (OutboxEventStatus.DONE,),
    batch_size: int = BATCH_SIZE,
) -> AsyncGenerator[tuple[int, ...]]:
    """Apply the selected events in batches like ``process_batch``'s; yield each batch's ids.

    Writes nothing to Postgres. Raises the first ``EventError`` or ``QLeverError``.
    """
    last = after_id
    wanted = sorted(OutboxEventStatus(status).value for status in statuses)
    while True:
        rows = await conn.fetch(
            f"SELECT {EVENT_COLUMNS} FROM outbox_events "
            "WHERE id > $1 AND ($2::bigint IS NULL OR id <= $2) AND status::text = ANY($3::text[]) "
            "ORDER BY id LIMIT $4",
            last,
            through_id,
            wanted,
            batch_size,
        )
        if not rows:
            return
        plan = await _plan(conn, [event_from_row(row) for row in rows])
        if plan.head_error is not None:
            raise plan.head_error
        text = plan.text
        if text:
            await client.update(text)
        last = plan.events[-1].id
        yield plan.ids


async def replay(
    conn: asyncpg.Connection,
    client: QLeverClient,
    *,
    after_id: int,
    through_id: int | None = None,
    statuses: Collection[OutboxEventStatus] = (OutboxEventStatus.DONE,),
    batch_size: int = BATCH_SIZE,
) -> int:
    """Apply every event with ``after_id < id <= through_id`` in ``statuses`` to ``client``.

    In id order and in batches built like ``process_batch``'s. Writes nothing to Postgres.
    Returns the last id applied (``after_id`` if none); raises on the first failure, and a
    caller restarts from the last value it got.
    """
    last = after_id
    batches = replay_batches(
        conn,
        client,
        after_id=after_id,
        through_id=through_id,
        statuses=statuses,
        batch_size=batch_size,
    )
    async with contextlib.aclosing(batches):
        async for ids in batches:
            last = ids[-1]
    return last


# -- catch-up -----------------------------------------------------------------------------------


class _DeadLettered(Exception):
    """A ``done`` event became ``failed``: catch-up plans again from the watermark (item 30)."""


@dataclass
class _CatchUp:
    """The state of one ``catch_up`` call: the watermark it last read or wrote, and its counts."""

    conn: asyncpg.Connection
    client: QLeverClient
    metrics: sync_metrics.Metrics | None
    expected: str | None = None
    replayed: int = 0
    dead_letters: int = 0

    async def acknowledged(self, ids: Sequence[int], updates_sent: int) -> None:
        """One transaction: the watermark to ``max(ids)``, compared on write (item 28).

        Raises ``_StaleWatermark`` (rolled back) if someone changed it.
        """
        async with self.conn.transaction():
            self.expected = await _write_watermark(self.conn, self.expected, max(ids))
        self.replayed += len(ids)
        if self.metrics is not None:
            self.metrics.observe_catch_up(replayed=len(ids), updates_sent=updates_sent)

    async def dead_letter(self, event: Event, error: EventError) -> None:
        """Record ``event`` as a catch-up dead letter (item 29), log it, raise ``_DeadLettered``.

        Raises ``_StaleWatermark`` (nothing written) if the watermark changed, ``RecordingError``
        if the event was no longer ``done``.
        """
        stored = EventError(error.code, **error.params, catch_up=True)
        text = _redact(str(stored), self.client)
        await _dead_letter_done(self.conn, event.id, text, self.expected)
        self.dead_letters += 1
        if self.metrics is not None:
            self.metrics.observe_catch_up(dead_lettered=True)
        logger.error(
            "sync_dead_letter",
            event_id=event.id,
            record_id=str(event.record_id),
            error=text,
            during="catch_up",
        )
        raise _DeadLettered

    async def send(self, update: str) -> None:
        """Send ``update``; an error ``classify`` calls ``unavailable`` leaves ``catch_up``."""
        _ensure_open(self.conn)
        try:
            await self.client.update(update)
        except QLeverError as error:
            if classify(error) == "unavailable":
                raise
            raise _Rejected(error) from error

    async def judge(self, event: Event, update: str) -> None:
        """``update`` of ``event``, sent alone, was rejected: confirm it (items 26 and 35).

        ``confirm_rejection`` sends ``PROBE_UPDATE``, then ``update`` alone again. Returns when the
        second send is acknowledged (the event is then replayed like any other); dead-letters the
        event when it is rejected again. Raises ``UpdatesRefusedError`` when the probe is
        rejected, and an ``unavailable`` ``QLeverError`` unchanged.
        """
        again = await confirm_rejection(self.conn, self.client, update)
        if again is not None:
            await self.dead_letter(event, _rejection(event.id, again, self.client))

    async def one_by_one(self, plan: _Plan) -> None:
        """After a rejected combined update: each event alone, in order (item 27)."""
        for event, update in zip(plan.events, plan.updates, strict=True):
            sent = 0
            if update:
                try:
                    await self.send(update)
                except _Rejected:
                    await self.judge(event, update)
                sent = 1
            await self.acknowledged((event.id,), sent)

    async def batch(self, plan: _Plan) -> None:
        """Send one planned batch and record it; the head's translation error is a dead letter."""
        if plan.head_error is not None:
            await self.dead_letter(plan.events[0], plan.head_error)
        text = plan.text
        if text:
            try:
                await self.send(text)
            except _Rejected:
                if len(plan.events) > 1:
                    await self.one_by_one(plan)
                    return
                await self.judge(plan.events[0], plan.updates[0])
        await self.acknowledged(plan.ids, 1 if text else 0)

    async def round(self, watermark: int, highest: int) -> None:
        """Replay the ``done`` events with ``watermark < id <= highest``, in batches, in order."""
        after = watermark
        while True:
            rows = await self.conn.fetch(
                f"SELECT {EVENT_COLUMNS} FROM outbox_events "
                "WHERE status = 'done' AND id > $1 AND id <= $2 ORDER BY id LIMIT $3",
                after,
                highest,
                BATCH_SIZE,
            )
            if not rows:
                return
            plan = await _plan(self.conn, [event_from_row(row) for row in rows])
            await self.batch(plan)
            after = plan.events[-1].id


class _Rejected(Exception):
    """An update got a status ``classify`` calls ``rejected`` (inside ``catch_up`` only)."""

    def __init__(self, error: QLeverError) -> None:
        super().__init__(str(error))
        self.error = error


async def _dead_letter_done(
    conn: asyncpg.Connection, event_id: int, error: str, expected: str | None
) -> None:
    """One transaction: compare the watermark, then set the ``done`` event ``failed`` (item 29).

    ``attempts`` is not changed and nothing else is written. Raises ``_StaleWatermark`` or
    ``RecordingError`` (both rolled back).
    """
    async with conn.transaction():
        if await _read_watermark(conn, for_update=True) != expected:
            raise _StaleWatermark
        status = await conn.execute(
            "UPDATE outbox_events SET status = 'failed', processed_at = now(), last_error = $2 "
            "WHERE id = $1 AND status = 'done'",
            event_id,
            error,
        )
        if status != "UPDATE 1":
            raise RecordingError(f"outbox event {event_id} is no longer done: {status}")


async def catch_up(
    conn: asyncpg.Connection,
    client: QLeverClient,
    *,
    metrics: sync_metrics.Metrics | None = None,
) -> int:
    """Replay the ``done`` events above the watermark into the live index; return how many.

    Does nothing without a watermark key. Batches are planned as ``process_batch`` plans them;
    the watermark is written after each acknowledged batch, or event sent alone (compared on
    write; if someone changed it, catch-up starts again from the new value). An event that cannot
    be replayed (its translation fails, or QLever rejects it alone, accepts the probe, and
    rejects it again) becomes ``failed`` without moving the watermark, and catch-up plans again
    from the watermark (contract §7). ``metrics`` is told of each thing as it is committed.

    Raises ``WatermarkError`` if the watermark is not an integer, ``RecordingError``, a
    ``QLeverError`` that ``classify`` calls ``unavailable`` (``UpdatesRefusedError`` when the
    probe is rejected), and whatever ``asyncpg`` raises. Never an ``EventError``, and never a
    rejection.
    """
    state = _CatchUp(conn, client, metrics)
    while True:
        state.expected = await _read_watermark(conn)
        watermark = watermark_value(state.expected)
        if watermark is None:
            break
        highest: int | None = await conn.fetchval(
            "SELECT max(id) FROM outbox_events WHERE status = 'done'"
        )
        if highest is None or highest <= watermark:
            break
        logger.info("sync_catch_up", watermark=watermark, through=highest)
        try:
            await state.round(watermark, highest)
        except (_StaleWatermark, _DeadLettered):
            continue
    if state.replayed or state.dead_letters:
        log = logger.warning if state.dead_letters else logger.info
        log("sync_caught_up", events=state.replayed, dead_letters=state.dead_letters)
    return state.replayed


# -- the process --------------------------------------------------------------------------------


async def pause(seconds: float, stop: asyncio.Event) -> None:
    """Sleep ``seconds``, or less if ``stop`` is set: every sleep of the loop (tests patch it)."""
    if seconds <= 0 or stop.is_set():
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def _wait_for_work(wake: asyncio.Event, stop: asyncio.Event, seconds: float) -> None:
    """Wait for a notification, ``stop``, or the poll interval, whichever comes first."""
    waiters = {asyncio.ensure_future(wake.wait()), asyncio.ensure_future(stop.wait())}
    try:
        await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


DATABASE_ERRORS: Final = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    ConnectionLostError,
    OSError,
)
"""The errors every ``lae sync-worker`` command reports with ``report_database_error``."""


def report_database_error(
    error: BaseException, *, closed: bool, command: Command, token: str | None
) -> int:
    """Log a database error that ends ``command`` and return its exit code (items 39, 40, 43).

    One reporter for ``run``, ``--once``, ``--replay-after`` and ``--failed``. A lost connection
    is ``sync_connection_lost`` and 1; any other error is ``sync_worker_failed`` with its
    traceback, and 4 when ``database_failure`` calls it ``"persistent"``, else 1. Nothing is
    retried: the caller closes its connection and exits (item 42). ``token`` is removed from the
    text.
    """
    kind = database_failure(error, closed=closed)
    if kind == "connection_lost":
        logger.error(
            "sync_connection_lost",
            error=_redact_token(str(error), token),
            error_type=type(error).__name__,
            command=command,
        )
        return EXIT_FAILURE
    persistent = kind == "persistent"
    code = EXIT_PERSISTENT if persistent else EXIT_FAILURE
    logger.error(
        "sync_worker_failed",
        error=_redact_token(str(error), token),
        error_type=type(error).__name__,
        sqlstate=getattr(error, "sqlstate", None),
        persistent=persistent,
        exit_code=code,
        command=command,
        exc_info=error,
    )
    return code


def _config_problem(settings: Settings) -> str | None:
    if not settings.qlever_url:
        return "LAE_QLEVER_URL is not set"
    if settings.qlever_access_token is None or not settings.qlever_access_token.get_secret_value():
        return "LAE_QLEVER_ACCESS_TOKEN is not set (QLever refuses updates without it)"
    return None


def live_client(settings: Settings) -> QLeverClient:
    """The client of the live index: ``LAE_QLEVER_URL``, its update URL, the access token."""
    assert settings.qlever_url is not None and settings.qlever_access_token is not None
    return QLeverClient(
        settings.qlever_url,
        settings.qlever_update_url or settings.qlever_url,
        settings.qlever_access_token.get_secret_value(),
    )


class DatabaseUrlError(ValueError):
    """``LAE_DATABASE_URL`` cannot be parsed as a PostgreSQL URL (exit 2; contract §9).

    The message describes the parse error; it holds neither the URL nor its password.
    """


def _url_passwords(database_url: str) -> list[str]:
    """The password of ``database_url`` as written and decoded, as far as it can be found."""
    password = None
    with contextlib.suppress(ValueError):
        password = urlsplit(database_url).password
    found = {text for text in (password, unquote(password or "")) if text}
    return sorted(found, key=len, reverse=True)  # the longer first: neither leaves a piece


def _url_error(error: Exception, database_url: str) -> DatabaseUrlError:
    text = str(error) or type(error).__name__
    for password in _url_passwords(database_url):
        text = text.replace(password, _REDACTED)
    return DatabaseUrlError(f"LAE_DATABASE_URL is not a valid PostgreSQL URL: {text}")


async def connect(database_url: str) -> asyncpg.Connection:
    """A new ``asyncpg`` connection to ``database_url`` (an SQLAlchemy URL, ``LAE_DATABASE_URL``).

    Raises ``DatabaseUrlError`` when the URL cannot be parsed: by SQLAlchemy in ``asyncpg_dsn``
    (``ArgumentError``, or ``ValueError`` for a port that is not a number), or by asyncpg before
    it opens a socket (``ClientConfigurationError``, a ``ValueError``; or a ``ValueError`` of its
    own). A server that cannot be reached or refuses the login raises what ``asyncpg.connect``
    raises (``CONNECT_ERRORS``); a host name that does not resolve is one. An ``OSError`` is
    never a parse error, even when it is a ``ValueError`` too: ``ssl.SSLCertVerificationError``
    is both, and comes from the TLS handshake with a server that was reached (contract §10).
    """
    try:
        dsn = asyncpg_dsn(database_url)
    except (sqlalchemy.exc.ArgumentError, ValueError) as error:
        raise _url_error(error, database_url) from None
    try:
        return await asyncpg.connect(dsn)
    except OSError:  # the socket or the TLS handshake: the URL parsed
        raise
    except ValueError as error:  # asyncpg parses the DSN before any I/O
        raise _url_error(error, database_url) from None


CONNECT_ERRORS: Final = (
    OSError,
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    asyncpg.InternalClientError,
)
"""What ``asyncpg.connect`` raises for a URL that parsed: ``sync_database_unreachable``, exit 1.

``OSError``: no route, refused, timed out, a failed TLS handshake. ``PostgresError``: the server
refused the login or is starting. ``InterfaceError``, raised after the socket opened: the server
asks for an authentication method asyncpg does not have, or its SCRAM signature does not verify.
``InternalClientError``: no host has the ``target_session_attrs`` asked for, or the server broke
the protocol. Each depends on the server, so each may pass without a change to this host's
settings (contract §10 item 48)."""


async def _open_connection(settings: Settings, command: Command) -> asyncpg.Connection | int:
    """The command's connection, or its exit code after saying why there is none (§9 item 46).

    A URL that cannot be parsed refuses to start: one stderr line, ``EXIT_CONFIG_ERROR``. Every
    other failure to connect (``CONNECT_ERRORS``) is ``sync_database_unreachable`` and
    ``EXIT_FAILURE`` (items 39 and 48); ``--replay-after`` and ``--failed`` also say so on
    stderr.
    """
    try:
        return await connect(settings.database_url)
    except DatabaseUrlError as error:
        print(f"lae sync-worker: refusing to start: {error}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except CONNECT_ERRORS as error:
        logger.error(
            "sync_database_unreachable",
            error=str(error),
            error_type=type(error).__name__,
            command=command,
        )
        if command in ("replay", "failed"):
            print(f"lae sync-worker: database unreachable: {error}", file=sys.stderr)
        return EXIT_FAILURE


async def _try_lock(conn: asyncpg.Connection) -> bool:
    locked: bool = await conn.fetchval("SELECT pg_try_advisory_lock($1)", SYNC_WORKER_LOCK)
    return locked


async def _take_lock(conn: asyncpg.Connection, stop: asyncio.Event) -> bool:
    """Take the advisory lock, trying every ``LOCK_RETRY_S``; ``False`` if stopped first."""
    logged = False
    while not stop.is_set():
        if await _try_lock(conn):
            return True
        if not logged:
            logger.warning("sync_lock_held", retry_seconds=LOCK_RETRY_S)
            logged = True
        await pause(LOCK_RETRY_S, stop)
    return False


async def _catch_up_until_done(
    conn: asyncpg.Connection,
    client: QLeverClient,
    stop: asyncio.Event,
    metrics: sync_metrics.Metrics | None = None,
) -> None:
    """``catch_up``, retried with backoff while QLever is unavailable or refuses every update."""
    outages = 0
    while not stop.is_set():
        try:
            await catch_up(conn, client, metrics=metrics)
        except QLeverError as error:
            if classify(error) != "unavailable":
                raise
            outages += 1
            if metrics is not None:
                metrics.sync_unavailable_total += 1
            if isinstance(error, UpdatesRefusedError):
                logger.error(
                    "sync_updates_refused",
                    status=error.status,
                    body=redacted_body(error.body, client),
                    consecutive=outages,
                    during="catch_up",
                )
            else:
                logger.warning(
                    "sync_unavailable",
                    error=_redact(str(error), client),
                    during="catch_up",
                    consecutive=outages,
                )
            await pause(backoff_seconds(outages), stop)
            continue
        return


async def _loop(
    conn: asyncpg.Connection,
    client: QLeverClient,
    stop: asyncio.Event,
    metrics: sync_metrics.Metrics,
) -> None:
    """Process batches until ``stop`` is set (contract §3 items 20, 21, 23)."""
    wake = asyncio.Event()

    def notified(*_: object) -> None:
        wake.set()

    await conn.add_listener(OUTBOX_CHANNEL, notified)
    outages = 0
    while not stop.is_set():
        # Cleared before the count, not after: a notification that arrives while the count is
        # being read must still end the wait below, or its event waits for the next poll.
        wake.clear()
        await metrics.refresh(conn)
        if metrics.pending == 0:
            await _wait_for_work(wake, stop, POLL_INTERVAL_S)
            woke = asyncio.get_running_loop().time()
            if stop.is_set():
                break
            await metrics.refresh(conn)
            if metrics.pending == 0:
                if await _behind(conn):  # the watermark was lowered while the queue was empty
                    logger.warning("sync_stale_watermark", done=0)
                    await _catch_up_until_done(conn, client, stop, metrics)
                continue
            if metrics.pending < BATCH_SIZE:
                await pause(woke + BATCH_WINDOW_S - asyncio.get_running_loop().time(), stop)
                if stop.is_set():
                    break
        result = await process_batch(conn, client)
        metrics.observe(result)
        if result.outcome in ("empty", "done"):
            outages = 0
            if result.done:
                logger.info("sync_batch_done", events=len(result.done), last_id=result.done[-1])
        elif result.outcome == "unavailable":  # an outage, or the probe rejected (item 36)
            outages += 1
            if result.refusal is not None:
                logger.error(
                    "sync_updates_refused",
                    status=result.refusal.status,
                    body=result.refusal.body,
                    consecutive=outages,
                    during="live",
                )
            else:
                logger.warning("sync_unavailable", error=result.error, consecutive=outages)
            await pause(backoff_seconds(outages), stop)
        elif result.outcome == "stale_watermark":
            outages = 0
            logger.warning("sync_stale_watermark", done=len(result.done))
            await _catch_up_until_done(conn, client, stop, metrics)
        else:  # rejected
            outages = 0
            if not result.dead_lettered and result.failed_event is not None:
                attempts: int = await conn.fetchval(
                    "SELECT attempts FROM outbox_events WHERE id = $1", result.failed_event
                )
                await pause(backoff_seconds(attempts), stop)


async def _serve(
    conn: asyncpg.Connection,
    client: QLeverClient,
    settings: Settings,
    *,
    once: bool,
    stop: asyncio.Event,
) -> int:
    if once:
        if not await _try_lock(conn):
            logger.warning("sync_lock_held")
            return EXIT_FAILURE
    elif not await _take_lock(conn, stop):
        return 0
    if once:
        await catch_up(conn, client)
        result = await process_batch(conn, client)
        logger.info(
            "sync_once",
            outcome=result.outcome,
            events=len(result.done),
            failed_event=result.failed_event,
            error=result.error,
        )
        return 0 if result.outcome in ("empty", "done") else EXIT_FAILURE
    metrics = sync_metrics.Metrics()
    server = None
    if settings.sync_metrics_port is not None:  # serving before the first catch-up (§7.5)
        host, port = settings.sync_metrics_host, settings.sync_metrics_port
        try:  # after the lock: a worker waiting for it does not hold the port (§9 item 47)
            server = await sync_metrics.serve(metrics, host, port)
        except OSError as error:  # in use, not permitted, not an address of this host
            print(
                f"lae sync-worker: refusing to start: cannot listen for metrics on "
                f"{host}:{port}: {error}",
                file=sys.stderr,
            )
            return EXIT_CONFIG_ERROR
    try:
        await metrics.refresh(conn)
        await _catch_up_until_done(conn, client, stop, metrics)
        await _loop(conn, client, stop, metrics)
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
    return 0


async def run(
    settings: Settings,
    *,
    once: bool = False,
    client: QLeverClient | None = None,
    stop: asyncio.Event | None = None,
) -> int:
    """Run the worker until ``stop`` (or one batch with ``once``); return the exit code.

    ``client`` defaults to the live index from the settings; then ``LAE_QLEVER_URL`` and
    ``LAE_QLEVER_ACCESS_TOKEN`` are required (``EXIT_CONFIG_ERROR`` otherwise). Without ``stop``
    the process's ``SIGTERM`` and ``SIGINT`` stop it.
    """
    own_client = client is None
    if client is None:
        problem = _config_problem(settings)
        if problem is not None:
            print(f"lae sync-worker: refusing to start: {problem}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        client = live_client(settings)
    connected = await _open_connection(settings, "once" if once else "run")
    if isinstance(connected, int):
        if own_client:
            await client.aclose()
        return connected
    conn = connected
    loop = asyncio.get_running_loop()
    handle_signals = stop is None
    stopping = stop if stop is not None else asyncio.Event()
    if handle_signals:
        for signum in STOP_SIGNALS:
            loop.add_signal_handler(signum, stopping.set)
    logger.info("sync_worker_started", once=once)
    try:
        return await _serve(conn, client, settings, once=once, stop=stopping)
    except WatermarkError as error:
        print(f"lae sync-worker: refusing to start: {error}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except (RecordingError, EventError, QLeverError) as error:
        logger.error(
            "sync_worker_failed",
            error=_redact(str(error), client),
            error_type=type(error).__name__,
            persistent=False,
            exit_code=EXIT_FAILURE,
            command="once" if once else "run",
        )
        return EXIT_FAILURE
    except DATABASE_ERRORS as error:
        # A commit that fails leaves its batch pending (rolled back, or never committed once the
        # connection closes); the restarted worker sends it again (contract §4). Exit 4 only for
        # an error that cannot pass by itself; the unit does not restart after it (item 41).
        return report_database_error(
            error,
            closed=conn.is_closed(),
            command="once" if once else "run",
            token=client.access_token,
        )
    finally:
        if handle_signals:
            for signum in STOP_SIGNALS:
                loop.remove_signal_handler(signum)
        with contextlib.suppress(Exception):
            await conn.close(timeout=5)
        if own_client:
            await client.aclose()
        logger.info("sync_worker_stopped")


# -- command line -------------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The options of ``lae sync-worker`` (also used by ``lae.cli`` for its help)."""
    parser.add_argument(
        "--once",
        action="store_true",
        help="process one batch (after catching up) and exit: 0 if empty or done, 1 otherwise",
    )
    parser.add_argument(
        "--failed",
        action="store_true",
        help="print the dead letters (failed events) as JSON lines and exit; needs no QLever",
    )
    parser.add_argument(
        "--replay-after",
        type=int,
        metavar="ID",
        help="apply the done events with a higher id to the target and exit; writes nothing",
    )
    parser.add_argument("--through", type=int, metavar="ID", help="last event id to replay")
    parser.add_argument(
        "--target-url", metavar="URL", help="QLever to replay into (default: the live index)"
    )
    parser.add_argument(
        "--target-update-url", metavar="URL", help="its update endpoint (default: --target-url)"
    )


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    """Parse the arguments of ``lae sync-worker``."""
    parser = argparse.ArgumentParser(prog="lae sync-worker")
    add_arguments(parser)
    args = parser.parse_args(list(argv))
    replay_only = (args.through, args.target_url, args.target_update_url)
    if args.replay_after is None and any(value is not None for value in replay_only):
        parser.error("--through, --target-url and --target-update-url need --replay-after")
    if args.replay_after is not None and args.once:
        parser.error("--once and --replay-after cannot be combined")
    if args.failed and (args.once or args.replay_after is not None):
        parser.error("--failed cannot be combined with --once or --replay-after")
    if args.target_update_url is not None and args.target_url is None:
        parser.error("--target-update-url needs --target-url")
    return args


async def _replay_command(settings: Settings, args: argparse.Namespace) -> int:
    """``--replay-after``: replay into the target, print the last id applied."""
    problem = _config_problem(settings)
    if problem is not None and not (args.target_url and settings.qlever_access_token):
        print(f"lae sync-worker: refusing to start: {problem}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    token = settings.qlever_access_token
    assert token is not None
    if args.target_url:
        client = QLeverClient(
            args.target_url, args.target_update_url or args.target_url, token.get_secret_value()
        )
    else:
        client = live_client(settings)
    last = args.replay_after
    connected = await _open_connection(settings, "replay")
    if isinstance(connected, int):
        await client.aclose()
        return connected
    conn = connected
    holding = False  # the connection, and the lock when one is taken: the last id is printed
    try:
        if not args.target_url and not await _try_lock(conn):
            print("lae sync-worker: another worker holds the lock", file=sys.stderr)
            return EXIT_FAILURE
        holding = True
        batches = replay_batches(conn, client, after_id=args.replay_after, through_id=args.through)
        async with contextlib.aclosing(batches):
            async for ids in batches:
                last = ids[-1]
    except (EventError, QLeverError) as error:
        print(last)
        print(f"lae sync-worker: replay stopped: {_redact(str(error), client)}", file=sys.stderr)
        return EXIT_FAILURE
    except DATABASE_ERRORS as error:  # reported like run's (contract §8.5 item 43)
        code = report_database_error(
            error, closed=conn.is_closed(), command="replay", token=client.access_token
        )
        if holding:
            print(last)
        print(f"lae sync-worker: replay stopped: {_redact(str(error), client)}", file=sys.stderr)
        return code
    finally:
        with contextlib.suppress(Exception):
            await conn.close(timeout=5)
        await client.aclose()
    print(last)
    return 0


FAILED_FIELDS: Final = (
    "id",
    "kind",
    "record_id",
    "from_version",
    "to_version",
    "attempts",
    "created_at",
    "processed_at",
    "last_error",
)
"""The fields of each line ``--failed`` prints (contract §7.3)."""


async def _failed_command(settings: Settings) -> int:
    """``--failed``: one JSON object per ``failed`` event, ascending by id; only reads."""
    connected = await _open_connection(settings, "failed")
    if isinstance(connected, int):
        return connected
    conn = connected
    try:
        rows = await conn.fetch(
            "SELECT id, kind::text AS kind, record_id, from_version, to_version, attempts, "
            "created_at, processed_at, last_error FROM outbox_events "
            "WHERE status = 'failed' ORDER BY id"
        )
    except DATABASE_ERRORS as error:  # reported like run's; no row is printed (item 43)
        token = settings.qlever_access_token
        return report_database_error(
            error,
            closed=conn.is_closed(),
            command="failed",
            token=token.get_secret_value() if token is not None else None,
        )
    finally:
        with contextlib.suppress(Exception):
            await conn.close(timeout=5)
    for row in rows:
        line = {field: row[field] for field in FAILED_FIELDS}
        line["record_id"] = str(line["record_id"])
        for field in ("created_at", "processed_at"):
            if line[field] is not None:
                line[field] = line[field].isoformat()
        print(json.dumps(line, ensure_ascii=False))
    return 0


def main(settings: Settings, argv: Sequence[str] = ()) -> Coroutine[Any, Any, int]:
    """``lae sync-worker [--once | --replay-after ID ... | --failed]``; returns the exit code."""
    args = parse_arguments(argv)
    if args.failed:
        return _failed_command(settings)
    if args.replay_after is not None:
        return _replay_command(settings, args)
    return run(settings, once=args.once)
