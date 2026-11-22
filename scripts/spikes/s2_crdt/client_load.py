"""Spike S2 load generator: pycrdt clients over ``websockets`` against the S2 server.

Default run (the guide's scenario): 30 clients across 10 rooms for 5 minutes. Every client
applies one Y.Map set and one Y.Text insert per second (separate transactions) and updates its
awareness state every 2 s. Every 30 s, five clients (each in a different room) disconnect and
reconnect after ``--offline`` seconds, alternating graceful closes (awareness null-state, then a
close frame) and abrupt TCP aborts; they keep editing while offline.

Every payload carries the writer's wall-clock timestamp, so each receiver measures propagation
latency directly (all clients share one clock). At the end every client and the server room
are compared: decoded state vectors, content digests, every written text token present exactly
once, and every client's last Y.Map value present everywhere.

Run (from ``backend/``, with the S2 server on port 9507)::

    uv run --group spike python ../scripts/spikes/s2_crdt/client_load.py \\
        --out /tmp/lae-i7/s2-load.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import time
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from pycrdt import (
    Awareness,
    Doc,
    Map,
    MapEvent,
    Text,
    TextEvent,
    TransactionEvent,
    YMessageType,
    YSyncMessageType,
    create_awareness_message,
    create_sync_message,
    create_update_message,
    handle_sync_message,
    read_message,
)
from s2common import content_digest, decode_state_vector, summarize
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

# One Y.Text insert: [<writer>.<seq>@<epoch ms>.<tenths><o=online, l=offline at write>]
TOKEN_RE = re.compile(r"\[([a-z][a-z0-9]*)\.(\d+)@(\d+\.\d)([ol])\]")


def now_ms() -> float:
    """Wall-clock milliseconds (shared by every process on the host)."""
    return time.time() * 1000


@dataclass
class Recorder:
    """Raw observations collected by every client in this process."""

    # (kind, writer, receiver, t_written, t_received, via, written_online, receiver_since)
    deliveries: list[tuple[str, str, str, float, float, int, bool, float]] = field(
        default_factory=list
    )
    # (sender, seq) -> t_sent, and (receiver, sender, seq) -> t_received
    awareness_sent: dict[tuple[str, int], float] = field(default_factory=dict)
    awareness_recv: dict[tuple[str, str, int], float] = field(default_factory=dict)
    # client -> list of [t_open, t_close or None]
    intervals: dict[str, list[list[float | None]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    sessions: list[dict[str, Any]] = field(default_factory=list)
    disconnects: list[dict[str, Any]] = field(default_factory=list)
    presence_removed: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    client_loop_lag_ms: list[float] = field(default_factory=list)
    load_start_ms: float = float("inf")  # sessions opened after this are reconnects
    load_end_ms: float = float("inf")  # disconnects after this are the final shutdown


class LoadClient:
    """One simulated editor: a pycrdt Doc and Awareness synced over one WebSocket."""

    def __init__(self, name: str, room: str, base_url: str, rec: Recorder, seed: int) -> None:
        self.name = name
        self.room = room
        self.url = f"{base_url.rstrip('/')}/{room}"
        self.rec = rec
        self.rng = random.Random(seed)
        self.doc = Doc()
        self.fields = self.doc.get("fields", type=Map)
        self.notes = self.doc.get("notes", type=Text)
        self.awareness = Awareness(self.doc)
        self.awareness.set_local_state(None)  # no presence until connected
        self.seq = 0
        self.aw_seq = 0
        self.tokens_written: list[str] = []
        self.online = False
        self.connected_since = 0.0
        self.ws: ClientConnection | None = None
        self.msgs_sent = 0
        self.msgs_received = 0
        self.bytes_received = 0
        self._queue: asyncio.Queue[bytes] | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._applying: int | None = None
        self._synced = asyncio.Event()
        self._step2_bytes = 0
        self._cid_names: dict[int, str] = {}
        self._awareness_task: asyncio.Task[None] | None = None
        self.doc.observe(self._on_doc)
        self.fields.observe(self._on_fields)
        self.notes.observe(self._on_notes)
        self.awareness.observe(self._on_awareness)

    # ---- observers -------------------------------------------------------------------------

    def _on_doc(self, event: TransactionEvent) -> None:
        if self._applying is not None:
            return  # remote update: do not echo it back
        if self.online and self._queue is not None:
            self._queue.put_nowait(create_update_message(event.update))

    def _record(self, kind: str, writer: str, t_written: float, online: bool) -> None:
        assert self._applying is not None
        self.rec.deliveries.append(
            (
                kind,
                writer,
                self.name,
                t_written,
                now_ms(),
                self._applying,
                online,
                self.connected_since,
            )
        )

    def _on_fields(self, event: MapEvent[Any]) -> None:
        if self._applying is None:
            return
        for key, change in event.keys.items():
            value = change.get("newValue")
            if key != self.name and isinstance(value, dict) and "t" in value:
                writer = str(value.get("by", key))
                self._record("map", writer, float(value["t"]), bool(value.get("online", True)))

    def _on_notes(self, event: TextEvent) -> None:
        if self._applying is None:
            return
        for delta in event.delta:
            inserted = delta.get("insert")
            if isinstance(inserted, str):
                for m in TOKEN_RE.finditer(inserted):
                    if m.group(1) != self.name:
                        self._record("text", m.group(1), float(m.group(3)), m.group(4) == "o")

    def _on_awareness(self, topic: str, payload: tuple[dict[str, Any], Any]) -> None:
        if topic != "update":
            return
        changes, origin = payload
        if origin == "local":
            ids = changes["added"] + changes["updated"] + changes["removed"]
            if self.online and self._queue is not None:
                update = self.awareness.encode_awareness_update(ids)
                self._queue.put_nowait(create_awareness_message(update))
            return
        received = now_ms()
        for cid in changes["added"] + changes["updated"]:
            state = self.awareness.states.get(cid) or {}
            user = state.get("user") or {}
            sender = user.get("name")
            if not sender or sender == self.name:
                continue
            self._cid_names[cid] = sender
            key = (self.name, sender, int(state.get("seq", -1)))
            if "seq" in state and key not in self.rec.awareness_recv:
                # first receipt only: renewals re-send an unchanged state with an old timestamp
                self.rec.awareness_recv[key] = received
                self.rec.deliveries.append(
                    (
                        "awareness",
                        sender,
                        self.name,
                        float(state["t"]),
                        received,
                        -1,
                        True,
                        self.connected_since,
                    )
                )
        for cid in changes["removed"]:
            if cid in self._cid_names:
                self.rec.presence_removed.append(
                    {
                        "receiver": self.name,
                        "sender": self._cid_names[cid],
                        "t": received,
                        "origin": str(origin),
                    }
                )

    # ---- connection lifecycle --------------------------------------------------------------

    async def connect(self, retries: int = 20) -> dict[str, Any]:
        """Open the socket, run the y-sync handshake, announce presence; return timings."""
        t_start = now_ms()
        for attempt in range(retries):
            try:
                ws = await connect(self.url, max_size=None, open_timeout=10)
                break
            except OSError as exc:
                self.rec.errors.append(f"{self.name} connect attempt {attempt}: {exc!r}")
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError(f"{self.name}: could not connect to {self.url}")
        t_open = now_ms()
        self.ws = ws
        self._queue = asyncio.Queue()
        self._synced.clear()
        self.online = True
        self.connected_since = t_open
        self.rec.intervals[self.name].append([t_open, None])
        await ws.send(create_sync_message(self.doc))
        self._tasks = [
            asyncio.create_task(self._reader(ws)),
            asyncio.create_task(self._writer(ws, self._queue)),
        ]
        if self._awareness_task is None:
            self._awareness_task = asyncio.create_task(self.awareness.start())
        self.announce()
        await asyncio.wait_for(self._synced.wait(), timeout=30)
        session = {
            "client": self.name,
            "open_ms": t_open - t_start,
            "synced_ms": now_ms() - t_start,
            "sync_step2_bytes": self._step2_bytes,
            "t": t_open,
        }
        self.rec.sessions.append(session)
        return session

    def announce(self) -> None:
        """Publish this client's awareness state (y-websocket does this on every change)."""
        self.aw_seq += 1
        t = now_ms()
        state = {
            "user": {"name": self.name, "kind": "agent"},
            "seq": self.aw_seq,
            "t": t,
            "cursor": {"anchor": self.rng.randint(0, 10_000)},
        }
        if self.online:
            self.rec.awareness_sent[(self.name, self.aw_seq)] = t
        self.awareness.set_local_state(state)

    async def _reader(self, ws: ClientConnection) -> None:
        try:
            async for message in ws:
                if not isinstance(message, bytes):
                    continue
                self.msgs_received += 1
                self.bytes_received += len(message)
                if message[0] == YMessageType.SYNC:
                    self._applying = message[1]
                    try:
                        reply = handle_sync_message(message[1:], self.doc)
                    finally:
                        self._applying = None
                    if message[1] == YSyncMessageType.SYNC_STEP2 and not self._synced.is_set():
                        self._step2_bytes = len(message)
                        self._synced.set()
                    if reply is not None and self._queue is not None:
                        self._queue.put_nowait(reply)
                elif message[0] == YMessageType.AWARENESS:
                    self.awareness.apply_awareness_update(read_message(message[1:]), "remote")
        except ConnectionClosed:
            pass
        except Exception as exc:  # pragma: no cover - recorded, not raised
            self.rec.errors.append(f"{self.name} reader: {exc!r}")
        finally:
            self._mark_closed()

    async def _writer(self, ws: ClientConnection, queue: asyncio.Queue[bytes]) -> None:
        try:
            while True:
                message = await queue.get()
                await ws.send(message)
                self.msgs_sent += 1
        except ConnectionClosed:
            pass

    def _mark_closed(self) -> None:
        if self.online:
            self.online = False
        intervals = self.rec.intervals[self.name]
        if intervals and intervals[-1][1] is None:
            intervals[-1][1] = now_ms()

    async def disconnect(self, graceful: bool) -> None:
        """Leave: graceful sends an awareness null-state and a close frame; abrupt aborts TCP."""
        ws = self.ws
        if ws is None:
            return
        t = now_ms()
        self.online = False
        if graceful:
            self.awareness.set_local_state(None)
            cid = self.awareness.client_id
            try:
                await ws.send(
                    create_awareness_message(self.awareness.encode_awareness_update([cid]))
                )
                await ws.close()
            except ConnectionClosed:
                pass
        else:
            ws.transport.abort()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._mark_closed()
        self.ws = None
        self._queue = None
        self.rec.disconnects.append({"client": self.name, "graceful": graceful, "t": t})

    async def close(self) -> None:
        """Graceful disconnect and stop the awareness timer."""
        await self.disconnect(graceful=True)
        if self._awareness_task is not None:
            await self.awareness.stop()
            self._awareness_task.cancel()
            await asyncio.gather(self._awareness_task, return_exceptions=True)
            self._awareness_task = None

    # ---- edits -----------------------------------------------------------------------------

    def edit(self) -> None:
        """One Y.Map set and one Y.Text insert (two transactions), both timestamped."""
        self.seq += 1
        t = now_ms()
        self.fields[self.name] = {"seq": self.seq, "t": t, "online": self.online, "by": self.name}
        token = f"[{self.name}.{self.seq}@{t:.1f}{'o' if self.online else 'l'}]"
        text = str(self.notes)
        boundaries = [0] + [m.end() for m in re.finditer(r"\]", text)]
        self.notes.insert(self.rng.choice(boundaries), token)
        self.tokens_written.append(token)

    def snapshot(self) -> dict[str, Any]:
        """Decoded state vector, content digest and token census of the local document."""
        content = {"fields": self.fields.to_py() or {}, "notes": str(self.notes)}
        tokens = [m.group(0) for m in TOKEN_RE.finditer(content["notes"])]
        return {
            "client": self.name,
            "state_vector": decode_state_vector(self.doc.get_state()),
            "digest": content_digest(content),
            "fields": content["fields"],
            "tokens": tokens,
            "notes_len": len(content["notes"]),
            "update_bytes": len(self.doc.get_update()),
        }


# ---- orchestration -------------------------------------------------------------------------


def http_json(url: str, method: str = "GET") -> dict[str, Any]:
    """Tiny stdlib HTTP helper for the server's stats/admin endpoints."""
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())  # type: ignore[no-any-return]


async def http_json_async(url: str, method: str = "GET") -> dict[str, Any]:
    """Run ``http_json`` off the event loop."""
    return await asyncio.to_thread(http_json, url, method)


async def loop_lag_monitor(rec: Recorder, interval: float = 0.05) -> None:
    """Client-side event-loop lag, to separate client delays from server delays."""
    while True:
        start = time.perf_counter()
        await asyncio.sleep(interval)
        rec.client_loop_lag_ms.append((time.perf_counter() - start - interval) * 1000)


async def rss_sampler(http: str, samples: list[dict[str, Any]], t0: float, every: float) -> None:
    """Poll the server's RSS, CPU and client count."""
    while True:
        try:
            s = await http_json_async(f"{http}/stats")
            samples.append(
                {
                    "t_s": round(time.time() - t0, 1),
                    "rss_mb": round(s["rss_bytes"] / 2**20, 2),
                    "cpu_s": round(s["cpu_user_s"] + s["cpu_system_s"], 2),
                    "clients": s["clients"],
                    "rooms": s["rooms"],
                }
            )
        except Exception as exc:  # noqa: BLE001 - sampling must not stop the run
            samples.append({"t_s": round(time.time() - t0, 1), "error": repr(exc)})
        await asyncio.sleep(every)


def churn_plan(args: argparse.Namespace) -> list[tuple[float, str, bool]]:
    """(offset_s, client, graceful) for each disconnect: rotating rooms and clients."""
    plan: list[tuple[float, str, bool]] = []
    rounds = int((args.duration - args.offline - 1) // args.churn_every)
    for r in range(1, rounds + 1):
        for i in range(args.churn_clients):
            room = (r * args.churn_clients + i) % args.rooms
            client = room * args.clients_per_room + r % args.clients_per_room
            plan.append((r * args.churn_every, f"c{client:02d}", i % 2 == 0))
    return plan


def connected_during(intervals: list[list[float | None]], start: float, end: float) -> bool:
    """True when one connection interval covers [start, end]."""
    return any(o <= start and (c is None or c >= end) for o, c in intervals if o is not None)


def analyse(
    rec: Recorder,
    clients: list[LoadClient],
    snapshots: dict[str, dict[str, Any]],
    server_rooms: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Turn raw observations into the spike's result figures."""
    live: dict[str, list[float]] = defaultdict(list)
    catchup: dict[str, list[float]] = defaultdict(list)
    # Catch-up: for updates that could not travel live (written offline, or the receiver was
    # offline), the delay after the later of the writer's and the receiver's reconnect.
    catchup_after_reconnect: list[float] = []
    for kind, writer, _receiver, t_written, t_recv, via, online, since in rec.deliveries:
        latency = t_recv - t_written
        if kind == "awareness":
            live[kind].append(latency)
            continue
        if (
            via == YSyncMessageType.SYNC_UPDATE
            and online
            and since <= t_written
            and connected_during(rec.intervals[writer], t_written, t_recv)
        ):
            live[kind].append(latency)
            continue
        catchup[kind].append(latency)
        writer_opens = [o for o, _c in rec.intervals[writer] if o is not None and o <= t_recv]
        catchup_after_reconnect.append(t_recv - max([since, *writer_opens[-1:]]))

    # Awareness delivery ratio: every update sent while the sender stayed connected for 0.5 s
    # should reach every room peer that stayed connected for the 1 s after it was sent.
    by_room: dict[str, list[LoadClient]] = defaultdict(list)
    for c in clients:
        by_room[c.room].append(c)
    room_of = {c.name: c.room for c in clients}
    expected = delivered = 0
    missing: list[tuple[str, str, int]] = []
    for (sender, seq), t_sent in rec.awareness_sent.items():
        if not connected_during(rec.intervals[sender], t_sent, t_sent + 500):
            continue
        for peer in by_room[room_of[sender]]:
            if peer.name == sender or not connected_during(
                rec.intervals[peer.name], t_sent, t_sent + 1000
            ):
                continue
            expected += 1
            if (peer.name, sender, seq) in rec.awareness_recv:
                delivered += 1
            elif len(missing) < 20:
                missing.append((peer.name, sender, seq))

    # Graceful-leave presence removal latency at peers.
    churn = [d for d in rec.disconnects if d["t"] <= rec.load_end_ms]
    removal_latency: list[float] = []
    removal_by_origin: dict[str, int] = defaultdict(int)
    for d in churn:
        if not d["graceful"]:
            continue
        for r in rec.presence_removed:
            if r["sender"] == d["client"] and 0 <= r["t"] - d["t"] < 10_000:
                removal_latency.append(r["t"] - d["t"])
                removal_by_origin[r["origin"]] += 1
    abrupt_removals = [
        r
        for d in churn
        if not d["graceful"]
        for r in rec.presence_removed
        if r["sender"] == d["client"] and 0 <= r["t"] - d["t"] < args.offline * 1000 + 2000
    ]

    # Convergence and completeness, per room.
    rooms: dict[str, Any] = {}
    for room, members in sorted(by_room.items()):
        snaps = [snapshots[c.name] for c in members]
        server = server_rooms.get(f"/ws/{room}", {})
        written = sorted(t for c in members for t in c.tokens_written)
        token_lists = [s["tokens"] for s in snaps]
        last_values_ok = all(
            s["fields"].get(c.name, {}).get("seq") == c.seq for s in snaps for c in members
        )
        rooms[room] = {
            "state_vectors_equal": all(s["state_vector"] == snaps[0]["state_vector"] for s in snaps)
            and server.get("state_vector") == snaps[0]["state_vector"],
            "digests_equal": len({s["digest"] for s in snaps} | {server.get("digest")}) == 1,
            "tokens_written": len(written),
            "all_tokens_present_once": all(sorted(tl) == written for tl in token_lists),
            "last_map_values_present": last_values_ok,
            "notes_len": snaps[0]["notes_len"],
            "client_update_bytes": snaps[0]["update_bytes"],
            "server_update_bytes": server.get("update_bytes"),
        }

    sessions = rec.sessions
    reconnects = [s for s in sessions if s["t"] >= rec.load_start_ms]
    return {
        "latency_ms": {
            "map_live": summarize(live["map"]),
            "text_live": summarize(live["text"]),
            "all_doc_updates_live": summarize(live["map"] + live["text"]),
            "awareness_live": summarize(live["awareness"]),
            "doc_updates_catchup_total_age": summarize(catchup["map"] + catchup["text"]),
            "doc_updates_catchup_after_reconnect": summarize(catchup_after_reconnect),
        },
        "awareness_delivery": {
            "expected": expected,
            "delivered": delivered,
            "ratio": round(delivered / expected, 5) if expected else None,
            "missing_sample": missing,
        },
        "presence": {
            "graceful_leave_removal_ms": summarize(removal_latency),
            "graceful_leave_removal_origins": dict(removal_by_origin),
            "abrupt_leave_removals_within_offline_window": len(abrupt_removals),
        },
        "reconnect": {
            "count": len(reconnects),
            "open_ms": summarize([s["open_ms"] for s in reconnects]),
            "synced_ms": summarize([s["synced_ms"] for s in reconnects]),
            "sync_step2_bytes": summarize([float(s["sync_step2_bytes"]) for s in reconnects]),
            "disconnects_graceful": sum(1 for d in churn if d["graceful"]),
            "disconnects_abrupt": sum(1 for d in churn if not d["graceful"]),
        },
        "initial_connect": {
            "synced_ms": summarize([s["synced_ms"] for s in sessions if s not in reconnects]),
        },
        "rooms": rooms,
        "all_rooms_converged": all(
            r["state_vectors_equal"]
            and r["digests_equal"]
            and r["all_tokens_present_once"]
            and r["last_map_values_present"]
            for r in rooms.values()
        ),
        "client_loop_lag_ms": summarize(rec.client_loop_lag_ms),
        "client_errors": rec.errors[:20],
        "client_error_count": len(rec.errors),
        "messages": {
            "sent": sum(c.msgs_sent for c in clients),
            "received": sum(c.msgs_received for c in clients),
            "bytes_received": sum(c.bytes_received for c in clients),
        },
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the scenario and return the result document."""
    http = args.http.rstrip("/")
    rec = Recorder()

    await http_json_async(f"{http}/stats/reset", "POST")
    before = await http_json_async(f"{http}/gc", "POST")
    baseline = await http_json_async(f"{http}/stats")
    rooms = [f"{args.room_prefix}{i:02d}" for i in range(args.rooms)]
    clients = [
        LoadClient(
            f"c{r * args.clients_per_room + j:02d}", room, args.url, rec, args.seed * 1000 + j
        )
        for r, room in enumerate(rooms)
        for j in range(args.clients_per_room)
    ]
    by_name = {c.name: c for c in clients}
    lag_task = asyncio.create_task(loop_lag_monitor(rec))

    t_connect = time.time()
    await asyncio.gather(*(c.connect() for c in clients))
    connected = await http_json_async(f"{http}/stats")
    print(f"connected {len(clients)} clients in {time.time() - t_connect:.2f}s", flush=True)

    samples: list[dict[str, Any]] = []
    t0 = time.time()
    rec.load_start_ms = t0 * 1000
    end = t0 + args.duration
    sampler = asyncio.create_task(rss_sampler(http, samples, t0, args.sample_every))

    async def edit_loop(c: LoadClient) -> None:
        nxt = t0 + c.rng.random()
        while nxt < end:
            await asyncio.sleep(max(0.0, nxt - time.time()))
            c.edit()
            nxt += args.edit_every

    async def awareness_loop(c: LoadClient) -> None:
        nxt = t0 + c.rng.random() * args.awareness_every
        while nxt < end:
            await asyncio.sleep(max(0.0, nxt - time.time()))
            if c.online:
                c.announce()
            nxt += args.awareness_every

    async def churn(offset: float, name: str, graceful: bool) -> None:
        await asyncio.sleep(max(0.0, t0 + offset - time.time()))
        c = by_name[name]
        await c.disconnect(graceful)
        await asyncio.sleep(args.offline)
        await c.connect()

    async def progress() -> None:
        while time.time() < end:
            await asyncio.sleep(30)
            last = samples[-1] if samples else {}
            print(
                f"t={time.time() - t0:5.0f}s rss={last.get('rss_mb')}MB "
                f"clients={last.get('clients')} deliveries={len(rec.deliveries)}",
                flush=True,
            )

    plan = churn_plan(args)
    await asyncio.gather(
        *(edit_loop(c) for c in clients),
        *(awareness_loop(c) for c in clients),
        *(churn(o, n, g) for o, n, g in plan),
        progress(),
    )
    rec.load_end_ms = now_ms()
    await asyncio.sleep(args.settle)
    sampler.cancel()

    snapshots = {c.name: c.snapshot() for c in clients}
    detail = await http_json_async(f"{http}/stats?detail=1")
    end_connected = detail
    for c in clients:
        await c.close()
    await asyncio.sleep(1)
    after_leave = await http_json_async(f"{http}/stats")
    await http_json_async(f"{http}/gc", "POST")
    after_leave_gc = await http_json_async(f"{http}/stats")
    evicted = await http_json_async(f"{http}/rooms/evict-all", "POST")
    await http_json_async(f"{http}/gc", "POST")
    after_evict = await http_json_async(f"{http}/stats")
    lag_task.cancel()

    results = analyse(rec, clients, snapshots, detail["room_detail"], args)
    mb = 2**20
    rss = {
        "baseline_mb": round(baseline["rss_bytes"] / mb, 2),
        "after_connect_mb": round(connected["rss_bytes"] / mb, 2),
        "peak_during_run_mb": max((s.get("rss_mb", 0) for s in samples), default=None),
        "end_connected_mb": round(end_connected["rss_bytes"] / mb, 2),
        "after_all_left_mb": round(after_leave["rss_bytes"] / mb, 2),
        "after_all_left_gc_mb": round(after_leave_gc["rss_bytes"] / mb, 2),
        "after_evict_all_gc_mb": round(after_evict["rss_bytes"] / mb, 2),
        "per_room_end_connected_mb": round(
            (end_connected["rss_bytes"] - baseline["rss_bytes"]) / mb / args.rooms, 3
        ),
        "per_room_after_connect_mb": round(
            (connected["rss_bytes"] - baseline["rss_bytes"]) / mb / args.rooms, 3
        ),
        "timeline": samples,
    }
    return {
        "config": vars(args),
        "server_pid": baseline["pid"],
        "gc_before": before,
        "churn_plan": plan,
        "rss": rss,
        "server": {
            "cpu_s_during_run": round(
                end_connected["cpu_user_s"]
                + end_connected["cpu_system_s"]
                - connected["cpu_user_s"]
                - connected["cpu_system_s"],
                2,
            ),
            "loop_lag_ms": end_connected["loop_lag_ms"],
            "connects": end_connected["connects"],
            "disconnects": after_leave["disconnects"],
            "handled_exceptions": after_evict["handled_exceptions"],
            "log_errors": after_evict["log_errors"],
            "log_warnings": after_evict["log_warnings"],
            "error_samples": after_evict["error_samples"],
            "clients_after_all_left": after_leave["clients"],
            "rooms_after_all_left": after_leave["rooms"],
            "evicted": evicted["evicted"],
            "rooms_after_evict": after_evict["rooms"],
            "persist": after_evict.get("persist"),
        },
        **results,
    }


def main() -> None:
    """CLI entry point."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="ws://127.0.0.1:9507/ws")
    p.add_argument("--http", default="http://127.0.0.1:9507")
    p.add_argument("--rooms", type=int, default=10)
    p.add_argument("--clients-per-room", type=int, default=3)
    p.add_argument("--duration", type=float, default=300.0)
    p.add_argument("--edit-every", type=float, default=1.0)
    p.add_argument("--awareness-every", type=float, default=2.0)
    p.add_argument("--churn-every", type=float, default=30.0)
    p.add_argument("--churn-clients", type=int, default=5)
    p.add_argument("--offline", type=float, default=5.0)
    p.add_argument("--settle", type=float, default=5.0)
    p.add_argument("--sample-every", type=float, default=5.0)
    p.add_argument("--room-prefix", default="s2-room-")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", default=None, help="write the full result JSON here")
    args = p.parse_args()
    result = asyncio.run(run(args))
    text = json.dumps(result, indent=2, default=str)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    summary = {k: v for k, v in result.items() if k not in ("rooms", "churn_plan", "config")}
    summary["rss"] = {k: v for k, v in result["rss"].items() if k != "timeline"}
    print(json.dumps(summary, indent=2, default=str))
    sys.exit(0 if result["all_rooms_converged"] else 1)


if __name__ == "__main__":
    main()
