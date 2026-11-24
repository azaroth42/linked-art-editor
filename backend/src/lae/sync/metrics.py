"""Counters of the sync worker and its tiny ``GET /metrics`` listener (Step 1.11 contract §2.3).

Prometheus text format. Gauges: ``outbox_pending``, ``outbox_failed``, ``sync_lag_seconds`` (age
of the oldest pending event, 0 if none) and ``sync_watermark`` (``NaN`` without one). Counters:
``updates_sent_total``, ``sync_events_done_total``, ``sync_dead_letters_total``,
``sync_unavailable_total``, ``sync_catch_up_events_total`` and
``sync_catch_up_dead_letters_total``. The first four names are the guide's.

``outbox_failed`` above 0 is the alarm: at least one dead letter, made on the live path or in
catch-up (contract §7.5). Catch-up (``lae.sync.worker.catch_up``) reports through
``observe_catch_up`` as each thing is committed: the events it replayed, the updates QLever
acknowledged (a probe is not counted) and each dead letter, which also counts in
``sync_dead_letters_total``. A catch-up that is retried because QLever is unavailable or refuses
every update (the probe was rejected) adds to ``sync_unavailable_total``. The worker starts the
listener before its first catch-up, so all of this is visible during a long one.

The worker has one database connection and uses it for everything (contract §3 item 22), so the
listener never queries: the loop calls ``Metrics.refresh`` once per iteration and the listener
renders that snapshot (the lag is computed at render time). ``outbox_failed`` needs a scan of
``outbox_events`` (no index on ``status`` other than the pending one) and is refreshed at most
every ``FAILED_REFRESH_S``. The listener answers ``GET /metrics`` and nothing else; bind it to
an internal interface (``LAE_SYNC_METRICS_HOST``, default ``127.0.0.1``).
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import asyncpg

from lae.bootstrap.common import QLEVER_INDEX_WATERMARK

if TYPE_CHECKING:
    from lae.sync.worker import BatchResult

FAILED_REFRESH_S: Final = 10.0
CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"
_READ_TIMEOUT_S: Final = 5.0

_GAUGES: Final = (
    ("outbox_pending", "Outbox events waiting to be sent to QLever."),
    ("outbox_failed", "Outbox events given up after the maximum number of attempts."),
    ("sync_lag_seconds", "Age of the oldest pending outbox event in seconds (0 if none)."),
    ("sync_watermark", "Highest outbox event id the live QLever index has acknowledged."),
)
_COUNTERS: Final = (
    ("updates_sent_total", "SPARQL updates QLever acknowledged."),
    ("sync_events_done_total", "Outbox events marked done."),
    ("sync_dead_letters_total", "Outbox events that became dead letters."),
    ("sync_unavailable_total", "Batches not sent because QLever was unavailable."),
    ("sync_catch_up_events_total", "Done outbox events replayed by catch-up."),
    ("sync_catch_up_dead_letters_total", "Done outbox events catch-up could not replay."),
)


@dataclass
class Metrics:
    """The worker's counters and the last snapshot of the outbox."""

    pending: int = 0
    failed: int = 0
    oldest_pending: datetime | None = None
    watermark: int | None = None
    updates_sent_total: int = 0
    sync_events_done_total: int = 0
    sync_dead_letters_total: int = 0
    sync_unavailable_total: int = 0
    sync_catch_up_events_total: int = 0
    sync_catch_up_dead_letters_total: int = 0
    _failed_at: float = field(default=-math.inf, repr=False)

    def observe(self, result: BatchResult) -> None:
        """Add what one ``process_batch`` call did to the counters."""
        self.updates_sent_total += result.updates_sent
        self.sync_events_done_total += len(result.done)
        if result.dead_lettered:
            self._dead_lettered()
        if result.outcome == "unavailable":
            self.sync_unavailable_total += 1

    def observe_catch_up(
        self, *, replayed: int = 0, updates_sent: int = 0, dead_lettered: bool = False
    ) -> None:
        """Add what catch-up just committed: events replayed, updates acknowledged, dead letter."""
        self.sync_catch_up_events_total += replayed
        self.updates_sent_total += updates_sent
        if dead_lettered:
            self.sync_catch_up_dead_letters_total += 1
            self._dead_lettered()

    def _dead_lettered(self) -> None:
        """One more dead letter: counted, shown at once, and recounted at the next refresh."""
        self.sync_dead_letters_total += 1
        self.failed += 1
        self._failed_at = -math.inf

    async def refresh(self, conn: asyncpg.Connection) -> None:
        """Read the gauges from Postgres on the worker's connection."""
        row = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM outbox_events WHERE status = 'pending') AS pending,
                   (SELECT created_at FROM outbox_events WHERE status = 'pending'
                    ORDER BY id LIMIT 1) AS oldest,
                   (SELECT value::text FROM sync_state WHERE key = $1) AS watermark
            """,
            QLEVER_INDEX_WATERMARK,
        )
        assert row is not None
        self.pending = row["pending"]
        self.oldest_pending = row["oldest"]
        self.watermark = None
        with contextlib.suppress(ValueError, TypeError):
            self.watermark = int(row["watermark"])
        now = time.monotonic()
        if now - self._failed_at >= FAILED_REFRESH_S:
            self.failed = await conn.fetchval(
                "SELECT count(*) FROM outbox_events WHERE status = 'failed'"
            )
            self._failed_at = now

    def lag_seconds(self, now: datetime | None = None) -> float:
        """Age of the oldest pending event, 0 if none."""
        if self.oldest_pending is None:
            return 0.0
        now = now or datetime.now(UTC)
        return max(0.0, (now - self.oldest_pending).total_seconds())

    def render(self, now: datetime | None = None) -> str:
        """The Prometheus text exposition of every metric."""
        gauges = {
            "outbox_pending": float(self.pending),
            "outbox_failed": float(self.failed),
            "sync_lag_seconds": self.lag_seconds(now),
            "sync_watermark": math.nan if self.watermark is None else float(self.watermark),
        }
        lines: list[str] = []
        for name, help_text in _GAUGES:
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
            lines.append(f"{name} {_number(gauges[name])}")
        for name, help_text in _COUNTERS:
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} counter"]
            lines.append(f"{name} {getattr(self, name)}")
        return "\n".join(lines) + "\n"


def _number(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    return str(int(value)) if value.is_integer() else f"{value:.3f}"


def _response(status: str, body: str, content_type: str = "text/plain; charset=utf-8") -> bytes:
    payload = body.encode("utf-8")
    head = (
        f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
    )
    return head.encode("ascii") + payload


async def _handle(
    metrics: Metrics, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        async with asyncio.timeout(_READ_TIMEOUT_S):
            request_line = await reader.readline()
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
        parts = request_line.decode("latin-1").split()
        if len(parts) >= 2 and parts[0] == "GET" and parts[1].split("?", 1)[0] == "/metrics":
            writer.write(_response("200 OK", metrics.render(), CONTENT_TYPE))
        else:
            writer.write(_response("404 Not Found", "not found\n"))
        await writer.drain()
    except (TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


async def serve(metrics: Metrics, host: str, port: int) -> asyncio.Server:
    """Start the ``/metrics`` listener on ``host:port``; the caller closes the server."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle(metrics, reader, writer)

    return await asyncio.start_server(handle, host, port)
