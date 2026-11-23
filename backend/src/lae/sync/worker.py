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
failure; the one event rejected alone (or whose translation raises at the head of a batch) gets
``attempts + 1`` and ``last_error``, and after ``MAX_ATTEMPTS`` becomes ``failed`` (a dead letter)
and the queue moves on. The record's next event then replaces the record's triples over the
versions the dead letter left uncertain (``lae.sync.events``).

**Catch-up** (``catch_up``). When the watermark exists and a ``done`` event has a higher id (an
index rebuilt from an older export, a restored index directory), those events are replayed
through ``replay`` before any pending event, and the watermark is written after each
acknowledged batch.

**Replay** (``replay``, ``apply_event``). Both take any ``QLeverClient`` as the target and write
nothing to Postgres, so Step 2.18 can replay into a second index. ``process_batch`` and
``catch_up`` are the only writers of ``outbox_events`` and ``sync_state``.

**Process** (``run``). One ``asyncpg`` connection holds ``pg_try_advisory_lock(SYNC_WORKER_LOCK)``,
listens on ``outbox`` and makes every read and write. A notification or the 1 s poll wakes the
loop; with ``BATCH_SIZE`` or more events pending it takes a batch at once, otherwise it first
waits until ``BATCH_WINDOW_S`` after waking. If the connection is lost the process exits 1 and
never reconnects in-process (a supervisor restarts it), so a lost lock is never used. ``SIGTERM``
and ``SIGINT`` let the batch in progress finish and exit 0. ``/metrics`` (``lae.sync.metrics``) is
served from a snapshot the loop refreshes, so the listener needs no connection of its own.

The access token goes with every update and is removed from every error text the worker logs or
stores.
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

import asyncpg

from lae.bootstrap.common import QLEVER_INDEX_WATERMARK, asyncpg_dsn
from lae.db.models import OutboxEventStatus
from lae.logging import get_logger
from lae.settings import Settings
from lae.sync import metrics as sync_metrics
from lae.sync.errors import EventError, rejected
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

EXIT_CONFIG_ERROR: Final = 2
"""Exit code of a worker that refuses to start (the same value as ``lae.cli.EXIT_CONFIG_ERROR``)."""

EXIT_FAILURE: Final = 1
"""Exit code when the lock is held (``--once``), the connection is lost, or recording failed."""

OUTBOX_CHANNEL: Final = "outbox"
STOP_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)

_UNAVAILABLE_STATUSES: Final = frozenset({401, 403, 408, 429})
_REDACTED: Final = "[redacted]"

logger = get_logger(__name__)

Outcome = Literal["empty", "done", "rejected", "unavailable", "stale_watermark"]


@dataclass(frozen=True, slots=True)
class BatchResult:
    """What one ``process_batch`` call did."""

    outcome: Outcome
    done: tuple[int, ...]  # ids marked done by this call, ascending
    failed_event: int | None  # the event that got an attempt counted, if any
    dead_lettered: bool  # that event is now `failed`
    updates_sent: int  # HTTP updates QLever acknowledged
    error: str | None


class WatermarkError(ValueError):
    """``sync_state.qlever_index_watermark`` exists but is not a JSON integer."""


class RecordingError(RuntimeError):
    """The transaction recording a batch or an attempt did not change the rows it must."""


class ConnectionLostError(RuntimeError):
    """The worker's one Postgres connection is closed; nothing more may be sent."""


class _StaleWatermark(Exception):
    """The watermark differs from the value read before the batch (rolls the transaction back)."""


# -- classification and backoff -----------------------------------------------------------------


def classify(error: QLeverError) -> Literal["unavailable", "rejected"]:
    """``unavailable`` without a status and for 401, 403, 408, 429 and 5xx; else ``rejected``."""
    status = error.status
    if status is None or status in _UNAVAILABLE_STATUSES or 500 <= status <= 599:
        return "unavailable"
    return "rejected"


def backoff_seconds(failures: int) -> float:
    """``min(2 ** (failures - 1), 30)``: 1, 2, 4, 8, 16, 30, 30, ... seconds."""
    return float(min(2 ** max(failures - 1, 0), MAX_BACKOFF_S))


def _redact(text: str, client: QLeverClient) -> str:
    token = client.access_token
    return text.replace(token, _REDACTED) if token else text


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
    )
    return BatchResult("rejected", done, event.id, dead, updates_sent, error)


async def _send_one_by_one(
    conn: asyncpg.Connection, client: QLeverClient, plan: _Plan, expected: str | None
) -> BatchResult:
    """After a rejected batch: send its events alone, in order, recording each (item 21)."""
    done: list[int] = []
    sent = 0
    for event, update in zip(plan.events, plan.updates, strict=True):
        if update:
            _ensure_open(conn)
            try:
                await client.update(update)
            except QLeverError as error:
                text = _redact(str(error), client)
                if classify(error) == "unavailable":
                    return BatchResult("unavailable", tuple(done), None, False, sent, text)
                failure = _redact(str(rejected(event.id, error.status, error.body)), client)
                return await _attempt_failed(
                    conn, event, failure, done=tuple(done), updates_sent=sent
                )
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
                return BatchResult("unavailable", (), None, False, 0, _redact(str(error), client))
            if len(plan.events) > 1:
                return await _send_one_by_one(conn, client, plan, expected)
            event = plan.events[0]
            failure = _redact(str(rejected(event.id, error.status, error.body)), client)
            return await _attempt_failed(conn, event, failure)
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


async def catch_up(conn: asyncpg.Connection, client: QLeverClient) -> int:
    """Replay the ``done`` events above the watermark into the live index; return how many.

    Does nothing without a watermark key. Writes the watermark after each acknowledged batch
    (compared on write; if someone changed it, starts again from the new value). Raises
    ``WatermarkError`` if it is not an integer, and the first ``EventError`` or ``QLeverError``.
    """
    replayed = 0
    while True:
        expected = await _read_watermark(conn)
        watermark = watermark_value(expected)
        if watermark is None:
            return replayed
        highest: int | None = await conn.fetchval(
            "SELECT max(id) FROM outbox_events WHERE status = 'done'"
        )
        if highest is None or highest <= watermark:
            return replayed
        logger.info("sync_catch_up", watermark=watermark, through=highest)
        batches = replay_batches(conn, client, after_id=watermark, through_id=highest)
        try:
            async with contextlib.aclosing(batches):
                async for ids in batches:
                    async with conn.transaction():
                        expected = await _write_watermark(conn, expected, ids[-1])
                    replayed += len(ids)
        except _StaleWatermark:
            continue
        return replayed


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
    conn: asyncpg.Connection, client: QLeverClient, stop: asyncio.Event
) -> None:
    """``catch_up``, retried with backoff while QLever is unavailable."""
    outages = 0
    while not stop.is_set():
        try:
            replayed = await catch_up(conn, client)
        except QLeverError as error:
            if classify(error) != "unavailable":
                raise
            outages += 1
            logger.warning("sync_unavailable", error=_redact(str(error), client), during="catch_up")
            await pause(backoff_seconds(outages), stop)
            continue
        if replayed:
            logger.info("sync_caught_up", events=replayed)
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
                    await _catch_up_until_done(conn, client, stop)
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
        elif result.outcome == "unavailable":
            outages += 1
            logger.warning("sync_unavailable", error=result.error, consecutive=outages)
            await pause(backoff_seconds(outages), stop)
        elif result.outcome == "stale_watermark":
            outages = 0
            logger.warning("sync_stale_watermark", done=len(result.done))
            await _catch_up_until_done(conn, client, stop)
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
    await _catch_up_until_done(conn, client, stop)
    metrics = sync_metrics.Metrics()
    server = None
    if settings.sync_metrics_port is not None:
        server = await sync_metrics.serve(
            metrics, settings.sync_metrics_host, settings.sync_metrics_port
        )
    try:
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
    loop = asyncio.get_running_loop()
    handle_signals = stop is None
    stopping = stop if stop is not None else asyncio.Event()
    if handle_signals:
        for signum in STOP_SIGNALS:
            loop.add_signal_handler(signum, stopping.set)
    try:
        conn = await asyncpg.connect(asyncpg_dsn(settings.database_url))
    except (OSError, asyncpg.PostgresError) as error:
        logger.error("sync_database_unreachable", error=str(error))
        if own_client:
            await client.aclose()
        return EXIT_FAILURE
    logger.info("sync_worker_started", once=once)
    try:
        return await _serve(conn, client, settings, once=once, stop=stopping)
    except WatermarkError as error:
        print(f"lae sync-worker: refusing to start: {error}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except (
        asyncpg.PostgresConnectionError,
        asyncpg.InterfaceError,
        ConnectionLostError,
        OSError,
    ) as error:
        logger.error("sync_connection_lost", error=str(error))
        return EXIT_FAILURE
    except (RecordingError, EventError, QLeverError) as error:
        logger.error("sync_worker_failed", error=_redact(str(error), client))
        return EXIT_FAILURE
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
    try:
        conn = await asyncpg.connect(asyncpg_dsn(settings.database_url))
    except (OSError, asyncpg.PostgresError) as error:
        print(f"lae sync-worker: database unreachable: {error}", file=sys.stderr)
        await client.aclose()
        return EXIT_FAILURE
    try:
        if not args.target_url and not await _try_lock(conn):
            print("lae sync-worker: another worker holds the lock", file=sys.stderr)
            return EXIT_FAILURE
        batches = replay_batches(conn, client, after_id=args.replay_after, through_id=args.through)
        async with contextlib.aclosing(batches):
            async for ids in batches:
                last = ids[-1]
    except (EventError, QLeverError) as error:
        print(last)
        print(f"lae sync-worker: replay stopped: {_redact(str(error), client)}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        with contextlib.suppress(Exception):
            await conn.close(timeout=5)
        await client.aclose()
    print(last)
    return 0


def main(settings: Settings, argv: Sequence[str] = ()) -> Coroutine[Any, Any, int]:
    """``lae sync-worker [--once | --replay-after ID ...]``; the coroutine returns the exit code."""
    args = parse_arguments(argv)
    if args.replay_after is not None:
        return _replay_command(settings, args)
    return run(settings, once=args.once)
