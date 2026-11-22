"""Spike S2 probes: memory per resident room, and room eviction behaviour.

``rooms``: open ``--count`` rooms, each with one client holding a synthetic Linked-Art-shaped
record (naively hydrated: objects to Y.Map, arrays to Y.Array, ``content`` to Y.Text), and
sample server RSS with the clients connected, after they leave (rooms stay resident), and after
evicting every room.

``eviction``: what pycrdt-websocket does when a room is deleted (a) while clients are still
connected and (b) after they have left, followed by a reconnect that re-seeds the new room.

Run (from ``backend/``, with the S2 server on port 9507)::

    uv run --group spike python ../scripts/spikes/s2_crdt/probes.py rooms --count 200 --parts 4
    uv run --group spike python ../scripts/spikes/s2_crdt/probes.py eviction
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

from client_load import LoadClient, Recorder, http_json_async
from pycrdt import Array, Map, Text

MB = 2**20


def synthetic_record(i: int, parts: int) -> dict[str, Any]:
    """A HumanMadeObject-shaped record; ``parts`` scales names, identifiers and statements."""
    aat = "http://vocab.getty.edu/aat/"
    base = f"https://example.org/object/{i}"
    return {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": base,
        "type": "HumanMadeObject",
        "_label": f"Synthetic object {i}",
        "classified_as": [{"id": f"{aat}300033618", "type": "Type", "_label": "Painting"}],
        "identified_by": [
            {
                "type": "Name",
                "content": f"Synthetic object {i}, title variant {k}",
                "language": [{"id": f"{aat}300388277", "type": "Language", "_label": "English"}],
                "classified_as": [{"id": f"{aat}300404670", "type": "Type"}],
            }
            for k in range(parts * 2)
        ]
        + [
            {
                "type": "Identifier",
                "content": f"ACC.{i}.{k}",
                "classified_as": [{"id": f"{aat}300312355", "type": "Type"}],
            }
            for k in range(parts)
        ],
        "referred_to_by": [
            {
                "type": "LinguisticObject",
                "content": ("A long descriptive statement about the object. " * 8) + str(k),
                "classified_as": [{"id": f"{aat}300435416", "type": "Type"}],
            }
            for k in range(parts)
        ],
        "dimension": [
            {
                "type": "Dimension",
                "value": 10.5 + k,
                "unit": {"id": f"{aat}300379098", "type": "MeasurementUnit"},
                "classified_as": [{"id": f"{aat}300055644", "type": "Type"}],
            }
            for k in range(parts)
        ],
        "produced_by": {
            "type": "Production",
            "carried_out_by": [{"id": f"https://example.org/person/{i}", "type": "Person"}],
            "timespan": {
                "type": "TimeSpan",
                "begin_of_the_begin": "1650-01-01T00:00:00Z",
                "end_of_the_end": "1650-12-31T23:59:59Z",
            },
        },
    }


def hydrate(value: Any, key: str | None = None) -> Any:
    """Naive JSON -> pycrdt prelim types (not the Step 0.8 projection; size proxy only)."""
    if isinstance(value, dict):
        return Map({k: hydrate(v, k) for k, v in value.items()})
    if isinstance(value, list):
        return Array([hydrate(v) for v in value])
    if key == "content" and isinstance(value, str):
        return Text(value)
    return value


async def rooms_cycle(args: argparse.Namespace, cycle: int) -> dict[str, Any]:
    """Open ``--count`` rooms with one client each, then leave, then evict; sample RSS."""
    http = args.http.rstrip("/")
    await http_json_async(f"{http}/gc", "POST")
    base = await http_json_async(f"{http}/stats?objects=1")
    rec = Recorder()
    clients: list[LoadClient] = []
    t0 = time.time()
    for start in range(0, args.count, 50):
        batch = []
        for i in range(start, min(args.count, start + 50)):
            c = LoadClient(f"r{i}", f"{args.prefix}{cycle}-{i:04d}", args.url, rec, seed=i)
            c.doc["record"] = hydrate(synthetic_record(i, args.parts))
            batch.append(c)
        await asyncio.gather(*(c.connect() for c in batch))
        clients.extend(batch)
    open_s = time.time() - t0
    await asyncio.sleep(2)
    await http_json_async(f"{http}/gc", "POST")
    connected = await http_json_async(f"{http}/stats?detail=1&objects=1")
    server_update_bytes = [
        r["update_bytes"]
        for name, r in connected["room_detail"].items()
        if name.startswith(f"/ws/{args.prefix}{cycle}-")
    ]
    for c in clients:
        await c.close()
    await asyncio.sleep(1)
    await http_json_async(f"{http}/gc", "POST")
    resident = await http_json_async(f"{http}/stats")
    await http_json_async(f"{http}/rooms/evict-all", "POST")
    await asyncio.sleep(1)
    evicted = await http_json_async(f"{http}/stats?objects=1")
    n = args.count
    return {
        "cycle": cycle,
        "open_all_s": round(open_s, 2),
        "ydoc_update_bytes_per_room": round(sum(server_update_bytes) / len(server_update_bytes)),
        "rss_mb": {
            "baseline": round(base["rss_bytes"] / MB, 2),
            "rooms_with_clients": round(connected["rss_bytes"] / MB, 2),
            "rooms_resident_no_clients": round(resident["rss_bytes"] / MB, 2),
            "after_evict_all": round(evicted["rss_bytes"] / MB, 2),
        },
        "per_room_kb": {
            "with_client": round((connected["rss_bytes"] - base["rss_bytes"]) / n / 1024, 1),
            "resident_no_client": round((resident["rss_bytes"] - base["rss_bytes"]) / n / 1024, 1),
        },
        "live_objects": {
            "before": base["live_objects"],
            "with_rooms": connected["live_objects"],
            "after_evict": evicted["live_objects"],
        },
        "server_errors": {
            "handled_exceptions": evicted["handled_exceptions"],
            "log_errors": evicted["log_errors"],
        },
        "client_errors": rec.errors[:5],
    }


async def probe_rooms(args: argparse.Namespace) -> dict[str, Any]:
    """Memory per resident room; a second cycle shows whether evicted memory is reused."""
    cycles = [await rooms_cycle(args, k) for k in range(args.cycles)]
    return {
        "probe": "rooms",
        "count": args.count,
        "parts": args.parts,
        "record_json_bytes": len(json.dumps(synthetic_record(0, args.parts))),
        "cycles": cycles,
    }


async def wait_for(predicate: Any, limit_s: float) -> float | None:
    """Poll ``predicate`` every 10 ms; return elapsed ms or None on timeout."""
    t0 = time.time()
    while time.time() - t0 < limit_s:
        if predicate():
            return round((time.time() - t0) * 1000, 1)
        await asyncio.sleep(0.01)
    return None


async def probe_eviction(args: argparse.Namespace) -> dict[str, Any]:
    """Room deletion with and without connected clients."""
    http = args.http.rstrip("/")
    rec = Recorder()
    out: dict[str, Any] = {"probe": "eviction"}
    room = f"{args.prefix}evict-{int(time.time())}"
    key = f"/ws/{room}"
    a, b = (LoadClient(n, room, args.url, rec, seed=i) for i, n in enumerate(("ea", "eb")))
    await a.connect()
    await b.connect()
    for _ in range(5):
        a.edit()
        b.edit()
    out["before_eviction_b_sees_a_ms"] = await wait_for(
        lambda: (b.fields.get("ea") or {}).get("seq") == a.seq, 5
    )

    # (a) evict while both are connected
    ev = await http_json_async(f"{http}/rooms/ws/{room}/evict", "POST")
    out["evict_with_clients"] = ev
    a.edit()
    out["after_live_eviction_b_sees_a_ms"] = await wait_for(
        lambda: (b.fields.get("ea") or {}).get("seq") == a.seq, 3
    )
    out["after_live_eviction_sockets_open"] = {"a": a.online, "b": b.online}
    c = LoadClient("ec", room, args.url, rec, seed=3)
    await c.connect()
    out["newcomer_after_live_eviction_sees_old_state_ms"] = await wait_for(
        lambda: (c.fields.get("ea") or {}).get("seq") == a.seq, 3
    )
    c.edit()
    out["zombie_peer_sees_newcomer_ms"] = await wait_for(lambda: "ec" in b.fields, 3)
    for cl in (a, b, c):
        await cl.close()

    # (b) everyone leaves, room evicted, one client reconnects with its full doc
    await http_json_async(f"{http}/rooms/ws/{room}/evict", "POST")
    await a.connect()
    out["reseed_by_reconnect_synced"] = True
    d = LoadClient("ed", room, args.url, rec, seed=4)
    await d.connect()
    out["fresh_client_after_reseed_sees_state_ms"] = await wait_for(
        lambda: (d.fields.get("ea") or {}).get("seq") == a.seq, 5
    )
    server = (await http_json_async(f"{http}/stats?room={key}"))["room_detail"].get(key, {})
    out["server_room_after_reseed"] = {
        k: server.get(k) for k in ("clients", "update_bytes", "digest")
    }
    out["a_digest_equals_server"] = a.snapshot()["digest"] == server.get("digest")
    for cl in (a, d):
        await cl.close()
    await http_json_async(f"{http}/rooms/ws/{room}/evict", "POST")
    out["client_errors"] = rec.errors[:5]
    return out


def main() -> None:
    """CLI entry point."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("probe", choices=["rooms", "eviction"])
    p.add_argument("--url", default="ws://127.0.0.1:9507/ws")
    p.add_argument("--http", default="http://127.0.0.1:9507")
    p.add_argument("--count", type=int, default=200)
    p.add_argument("--parts", type=int, default=4)
    p.add_argument("--cycles", type=int, default=2)
    p.add_argument("--prefix", default="s2-probe-")
    p.add_argument("--out", default=None)
    args = p.parse_args()
    fn = probe_rooms if args.probe == "rooms" else probe_eviction
    result = asyncio.run(fn(args))
    text = json.dumps(result, indent=2, default=str)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
