"""Publish storm against a running stack (Phase 1 Step 1.22): publish-to-QLever latency under load.

Starts ``--rate`` publishes per second, open loop (a new one every ``1 / rate`` seconds whether or
not the earlier ones have finished), for ``--duration`` seconds. Each publish does what an editor
does: ``GET /api/records/{id}`` for the draft epoch, a y-sync WebSocket to ``crdt-ws``
(``/ws/rooms/{id}?epoch=``), one edit of the draft (the first ``Name``'s ``content`` gets a
suffix, through ``lae.projection.ops``, so the edit is a Yjs ``Text`` splice as in the browser),
then ``POST /api/records/{id}/publish``. Warnings the API reports (409 ``LAE-PUB-002``) are
accepted and the publish is sent once more, as the Publish dialog does after the editor ticks
them.

The records are bulk-loaded ``HumanMadeObject``, ``Person``, ``Place`` and ``Type`` records with
no open QA finding, picked deterministically (``ORDER BY id``). Publish ``i`` edits record
``i mod (rate x duration / 2)``, so every record is published twice, half the run apart: the
first publish of a bulk-loaded version and an ordinary second revision both occur, and no
record has two publishes in flight at once.

Measured:

- **Publish-to-QLever latency per outbox event**: ``processed_at - created_at`` of every outbox
  event the storm produced. ``created_at`` is the start of the publish transaction, so this is
  an upper bound of commit-to-acknowledged-by-QLever.
- **Visibility probe**: for every ``--probe-every``-th publish, the time from the publish
  response until an ``ASK`` on QLever's SPARQL endpoint sees the new name (polled every 25 ms).
- **Sync lag** from the sync worker's ``/metrics`` (``sync_lag_seconds``, the age of the oldest
  pending event), sampled every 100 ms during the storm and until the outbox drains.
- **QLever delta triples** from QLever's ``/metrics`` before and after (needs the access token).
- Publish HTTP latency, the end-to-end time of each publish, and every non-200 answer by code.

Run with the backend environment against an api, crdt-ws, sync worker (with
``LAE_SYNC_METRICS_PORT``) and QLever that share one database; the api and crdt-ws need a
``LAE_DEV_USER`` with the Editor role (no login). Local only: never in CI.

    uv run --project backend python scripts/load/publish_storm.py \\
        --api http://127.0.0.1:9035 --crdt-ws ws://127.0.0.1:9535 \\
        --metrics http://127.0.0.1:9835/metrics --qlever http://127.0.0.1:7035 \\
        --qlever-token "$LAE_QLEVER_ACCESS_TOKEN" --database-url "$LAE_DATABASE_URL" \\
        --out /tmp/storm.json

Prints a JSON summary; ``--out`` also keeps every per-publish row.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Final

import asyncpg
import httpx
from pycrdt import (
    Doc,
    YMessageType,
    YSyncMessageType,
    create_sync_message,
    create_update_message,
    handle_sync_message,
)
from websockets.asyncio.client import ClientConnection, connect

from lae.projection.ops import add_node, list_entries, set_scalar
from lae.sparql.terms import iri, string_literal

TYPES: Final = ("HumanMadeObject", "Person", "Place", "Type")
SYNC_TIMEOUT_S: Final = 30.0
PROBE_INTERVAL_S: Final = 0.025
PROBE_TIMEOUT_S: Final = 30.0
SAMPLE_INTERVAL_S: Final = 0.1
DRAIN_TIMEOUT_S: Final = 300.0
WARNINGS_NOT_ACCEPTED: Final = "LAE-PUB-002"
CRM: Final = "http://www.cidoc-crm.org/cidoc-crm/"


@dataclass
class Row:
    """One publish of the storm."""

    i: int
    record_id: str
    scheduled_s: float
    started_s: float = math.nan
    status: int | None = None
    code: str | None = None
    version: int | None = None
    retried_for_warnings: bool = False
    edit: str = ""
    publish_s: float = math.nan
    total_s: float = math.nan
    visible_s: float | None = None
    error: str | None = None
    responded_at: float = field(default=math.nan, repr=False)


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0..100); ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return round(ordered[rank - 1], 4)


def stats(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": round(max(values), 4) if values else None,
        "mean": round(sum(values) / len(values), 4) if values else None,
    }


def prometheus(text: str) -> dict[str, float]:
    """``name{labels} value`` lines of a Prometheus exposition, as ``{name{labels}: value}``."""
    found: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        with contextlib.suppress(ValueError):
            found[name] = float(value)
    return found


class Draft:
    """A y-sync client for one record's room: sync, one edit, a barrier."""

    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws
        self.doc: Doc[Any] = Doc()
        self._step2 = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        with contextlib.suppress(Exception):
            async for message in self.ws:
                if not isinstance(message, bytes) or message[0] != YMessageType.SYNC:
                    continue
                reply = handle_sync_message(message[1:], self.doc)
                if reply is not None:
                    await self.ws.send(reply)
                if message[1] == YSyncMessageType.SYNC_STEP2:
                    self._step2.set()

    async def sync(self) -> None:
        """Send sync step 1 and wait for the server's step 2.

        The room handles one socket's frames in order and persists an update before it reads
        the next frame, so a step 2 after an update means the update is in the draft.
        """
        self._step2.clear()
        await self.ws.send(create_sync_message(self.doc))
        await asyncio.wait_for(self._step2.wait(), SYNC_TIMEOUT_S)

    async def edit(self, i: int) -> tuple[str, str]:
        """Change the draft; return ``(what, new name content)``."""
        before = self.doc.get_state()
        names = list_entries(self.doc, "identified_by[Name]")
        if names:
            _, entry = names[0]
            content = f"{str(entry.get('content') or '').split(' ~storm')[0]} ~storm{i}"
            set_scalar(self.doc, "content", content, base=entry)
            what = "name"
        else:
            content = f"Storm statement {i}"
            add_node(self.doc, "identified_by[Name]", {"content": content})
            what = "new-name"
        await self.ws.send(create_update_message(self.doc.get_update(before)))
        return what, content

    async def close(self) -> None:
        await self.ws.close()
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader


async def probe(qlever: httpx.AsyncClient, record_uri: str, content: str) -> float | None:
    """Poll QLever until the record has a name with ``content``; seconds, or None on timeout."""
    query = (
        f"ASK {{ {iri(record_uri)} {iri(CRM + 'P1_is_identified_by')} ?name . "
        f"?name {iri(CRM + 'P190_has_symbolic_content')} {string_literal(content)} }}"
    )
    started = time.monotonic()
    while time.monotonic() - started < PROBE_TIMEOUT_S:
        response = await qlever.get(
            "/", params={"query": query}, headers={"Accept": "application/sparql-results+json"}
        )
        if response.status_code == 200 and response.json().get("boolean") is True:
            return time.monotonic() - started
        await asyncio.sleep(PROBE_INTERVAL_S)
    return None


async def publish_one(
    row: Row,
    record_uri: str,
    api: httpx.AsyncClient,
    qlever: httpx.AsyncClient,
    crdt_ws: str,
    *,
    origin: float,
    probe_it: bool,
) -> None:
    started = time.monotonic()
    row.started_s = round(started - origin, 4)
    try:
        response = await api.get(f"/api/records/{row.record_id}")
        response.raise_for_status()
        epoch = response.headers["x-draft-epoch"]
        async with connect(
            f"{crdt_ws}/ws/rooms/{row.record_id}?epoch={epoch}", max_size=None
        ) as ws:
            draft = Draft(ws)
            await draft.sync()
            row.edit, content = await draft.edit(row.i)
            await draft.sync()
            body: dict[str, Any] = {"summary": f"publish storm {row.i}", "accepted_warnings": []}
            sent = time.monotonic()
            response = await api.post(f"/api/records/{row.record_id}/publish", json=body)
            answer = response.json()
            if response.status_code == 409 and answer.get("code") == WARNINGS_NOT_ACCEPTED:
                row.retried_for_warnings = True
                body["accepted_warnings"] = answer["params"]["unaccepted"]
                response = await api.post(f"/api/records/{row.record_id}/publish", json=body)
                answer = response.json()
            done = time.monotonic()
            row.publish_s = round(done - sent, 4)
            row.status = response.status_code
            if response.status_code == 200:
                row.version = answer["version"]
                row.responded_at = done
            else:
                row.code = answer.get("code")
            await draft.close()
        row.total_s = round(time.monotonic() - started, 4)
        if probe_it and row.status == 200:
            row.visible_s = await probe(qlever, record_uri, content)
    except Exception as error:  # noqa: BLE001 - every failure is recorded, the storm goes on
        row.error = f"{type(error).__name__}: {error}"[:300]
        row.total_s = round(time.monotonic() - started, 4)


async def sample_metrics(
    client: httpx.AsyncClient, url: str, samples: list[dict[str, float]], origin: float
) -> None:
    while True:
        with contextlib.suppress(httpx.HTTPError):
            response = await client.get(url)
            values = prometheus(response.text)
            values["t"] = round(time.monotonic() - origin, 3)
            samples.append(values)
        await asyncio.sleep(SAMPLE_INTERVAL_S)


async def qlever_metrics(client: httpx.AsyncClient, token: str | None) -> dict[str, float]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with contextlib.suppress(httpx.HTTPError):
        response = await client.get("/metrics", headers=headers)
        if response.status_code == 200:
            return {k: v for k, v in prometheus(response.text).items() if "delta" in k}
    return {}


async def pick_records(conn: asyncpg.Connection, count: int) -> list[tuple[str, str]]:
    rows = await conn.fetch(
        """
        SELECT r.id::text, r.uri FROM records r
        WHERE r.status = 'active' AND r.version >= 1 AND r.type = ANY($1::text[])
          AND NOT EXISTS (SELECT 1 FROM crdt_drafts d WHERE d.record_id = r.id)
          AND NOT EXISTS (SELECT 1 FROM qa_findings f WHERE f.record_id = r.id
                          AND f.status = 'open')
        ORDER BY r.id LIMIT $2
        """,
        list(TYPES),
        count,
    )
    return [(row[0], row[1]) for row in rows]


async def run(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    dsn = re.sub(r"^postgresql\+asyncpg://", "postgresql://", args.database_url)
    conn = await asyncpg.connect(dsn)
    total = int(args.rate * args.duration)
    distinct = max(1, total // 2)
    records = await pick_records(conn, distinct)
    if len(records) < distinct:
        raise SystemExit(f"only {len(records)} publishable records, need {distinct}")
    first_event = await conn.fetchval("SELECT coalesce(max(id), 0) FROM outbox_events")

    limits = httpx.Limits(max_connections=None, max_keepalive_connections=200)
    timeout = httpx.Timeout(120.0)
    async with (
        httpx.AsyncClient(base_url=args.api, limits=limits, timeout=timeout) as api,
        httpx.AsyncClient(base_url=args.qlever, limits=limits, timeout=timeout) as qlever,
        httpx.AsyncClient(timeout=5.0) as metrics_client,
    ):
        delta_before = await qlever_metrics(qlever, args.qlever_token)
        samples: list[dict[str, float]] = []
        origin = time.monotonic()
        sampler = asyncio.create_task(sample_metrics(metrics_client, args.metrics, samples, origin))
        rows: list[Row] = []
        tasks: list[asyncio.Task[None]] = []
        for i in range(total):
            due = origin + i / args.rate
            await asyncio.sleep(max(0.0, due - time.monotonic()))
            record_id, record_uri = records[i % distinct]
            row = Row(i=i, record_id=record_id, scheduled_s=round(i / args.rate, 4))
            rows.append(row)
            tasks.append(
                asyncio.create_task(
                    publish_one(
                        row,
                        record_uri,
                        api,
                        qlever,
                        args.crdt_ws,
                        origin=origin,
                        probe_it=i % args.probe_every == 0,
                    )
                )
            )
        schedule_end = time.monotonic() - origin
        await asyncio.gather(*tasks)
        publishes_end = time.monotonic() - origin
        drain_started = time.monotonic()
        # The worker's /metrics snapshot can be one loop iteration old, so ask Postgres.
        pending_query = "SELECT count(*) FROM outbox_events WHERE id > $1 AND status = 'pending'"
        while time.monotonic() - drain_started < DRAIN_TIMEOUT_S:
            if await conn.fetchval(pending_query, first_event) == 0:
                break
            await asyncio.sleep(SAMPLE_INTERVAL_S)
        drained_at = time.monotonic() - origin
        await asyncio.sleep(1.0)  # one more metrics sample after the last event
        sampler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sampler
        delta_after = await qlever_metrics(qlever, args.qlever_token)

    events = await conn.fetch(
        """
        SELECT id, status::text, from_version, payload->>'drift_check' AS drift,
               extract(epoch FROM processed_at - created_at) AS latency
        FROM outbox_events WHERE id > $1 ORDER BY id
        """,
        first_event,
    )
    await conn.close()

    ok = [row for row in rows if row.status == 200]
    failed: dict[str, int] = {}
    for row in rows:
        if row.status != 200:
            key = row.code or (row.error or "").split(":")[0] or str(row.status)
            failed[key] = failed.get(key, 0) + 1
    latencies = [float(e["latency"]) for e in events if e["status"] == "done"]
    first_publish = [
        float(e["latency"]) for e in events if e["status"] == "done" and e["drift"] == "false"
    ]
    lag = [s["sync_lag_seconds"] for s in samples if "sync_lag_seconds" in s]
    probes = [row.visible_s for row in rows if row.visible_s is not None]
    summary = {
        "rate_per_s": args.rate,
        "duration_s": args.duration,
        "publishes_scheduled": total,
        "distinct_records": distinct,
        "record_types": list(TYPES),
        "schedule_end_s": round(schedule_end, 2),
        "publishes_end_s": round(publishes_end, 2),
        "outbox_drained_s": round(drained_at, 2),
        "achieved_publish_rate_per_s": round(len(ok) / publishes_end, 2),
        "publishes_ok": len(ok),
        "publishes_failed": failed,
        "retried_for_warnings": sum(row.retried_for_warnings for row in rows),
        "edits": {kind: sum(row.edit == kind for row in rows) for kind in ("name", "new-name")},
        "first_publish_of_loaded_version": sum(row.version == 2 for row in ok),
        "outbox_events": len(events),
        "outbox_events_by_status": {
            status: sum(e["status"] == status for e in events)
            for status in ("done", "pending", "failed")
        },
        "drift_check_events": sum(e["drift"] == "true" for e in events),
        "publish_to_qlever_s": stats(latencies),
        "publish_to_qlever_s_without_drift_check": stats(first_publish),
        "visibility_probe_s": {
            **stats([p for p in probes if p is not None]),
            "probes": sum(1 for row in rows if row.i % args.probe_every == 0 and row.status == 200),
            "timeouts": sum(
                1
                for row in rows
                if row.i % args.probe_every == 0 and row.status == 200 and row.visible_s is None
            ),
        },
        "sync_lag_seconds_metric": {**stats(lag), "samples": len(lag)},
        "outbox_pending_max": max((s.get("outbox_pending", 0) for s in samples), default=None),
        "publish_http_s": stats([row.publish_s for row in ok]),
        "publish_end_to_end_s": stats([row.total_s for row in ok]),
        "qlever_delta_before": delta_before,
        "qlever_delta_after": delta_after,
        "updates_sent_total": samples[-1].get("updates_sent_total") if samples else None,
    }
    detail = {
        "rows": [{k: v for k, v in asdict(row).items() if k != "responded_at"} for row in rows],
        "metric_samples": samples,
    }
    return summary, detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", required=True, help="api base URL")
    parser.add_argument("--crdt-ws", required=True, help="crdt-ws base URL (ws://...)")
    parser.add_argument("--metrics", required=True, help="sync worker /metrics URL")
    parser.add_argument("--qlever", required=True, help="QLever base URL")
    parser.add_argument("--qlever-token", help="QLever access token (for its /metrics)")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--rate", type=float, default=20.0, help="publishes started per second")
    parser.add_argument("--duration", type=float, default=120.0, help="seconds")
    parser.add_argument("--probe-every", type=int, default=5, help="probe every n-th publish")
    parser.add_argument("--out", help="JSON file for the summary and every row")
    args = parser.parse_args(argv)
    summary, detail = asyncio.run(run(args))
    print(json.dumps(summary, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, **detail}, handle, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
