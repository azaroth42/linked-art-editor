"""Spike S2 browser-check peer: one pycrdt client driven by JSON lines on stdin.

``browser_check.mjs`` spawns this process and sends one command per line; each command is
answered with exactly one JSON line on stdout. Commands: ``connect``, ``write`` (``n``,
``every``), ``announce``, ``wait_seq`` (``key``, ``seq``, ``timeout``), ``presence``,
``removed``, ``snapshot``, ``disconnect`` (``graceful``), ``quit``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from client_load import LoadClient, Recorder, now_ms
from pycrdt import YSyncMessageType
from s2common import summarize


async def watch_presence(client: LoadClient, first_seen: dict[str, float]) -> None:
    """Record when each remote user name first appears in this client's awareness."""
    while True:
        for state in list(client.awareness.states.values()):
            name = (state or {}).get("user", {}).get("name")
            if name and name != client.name and name not in first_seen:
                first_seen[name] = now_ms()
        await asyncio.sleep(0.01)


async def handle(client: LoadClient, rec: Recorder, cmd: dict[str, Any], seen: dict[str, float]):
    """Execute one command and return its JSON-able reply."""
    op = cmd["cmd"]
    if op == "connect":
        return {"session": await client.connect()}
    if op == "write":
        for _ in range(int(cmd.get("n", 1))):
            client.edit()
            await asyncio.sleep(float(cmd.get("every", 0.05)))
        return {"seq": client.seq}
    if op == "announce":
        client.announce()
        return {"aw_seq": client.aw_seq, "t": now_ms()}
    if op == "wait_seq":
        deadline = time.time() + float(cmd.get("timeout", 10))
        t0 = now_ms()
        while time.time() < deadline:
            value = client.fields.get(cmd["key"])
            if isinstance(value, dict) and value.get("seq", -1) >= cmd["seq"]:
                return {"ok": True, "waited_ms": now_ms() - t0, "value": value}
            await asyncio.sleep(0.01)
        return {"ok": False, "value": client.fields.get(cmd["key"])}
    if op == "presence":
        states = {str(k): v for k, v in client.awareness.states.items()}
        return {"first_seen": seen, "states": states}
    if op == "snapshot":
        snap = client.snapshot()
        live = [
            t_recv - t_written
            for kind, _w, _r, t_written, t_recv, via, online, since in rec.deliveries
            if kind in ("map", "text")
            and via == YSyncMessageType.SYNC_UPDATE
            and online
            and since <= t_written
        ]
        value_types = {
            key: {k: type(v).__name__ for k, v in value.items()}
            for key, value in snap["fields"].items()
            if isinstance(value, dict)
        }
        return {
            **snap,
            "notes": str(client.notes),
            "live_latency_ms": summarize(live),
            "value_types": value_types,
            "client_id": client.doc.client_id,
        }
    if op == "disconnect":
        t_start = now_ms()
        await client.disconnect(bool(cmd.get("graceful", True)))
        return {"t_start": t_start, "t": now_ms()}
    if op == "removed":
        return {"removed": rec.presence_removed}
    if op == "quit":
        await client.close()
        return {"t": now_ms()}
    return {"error": f"unknown command {op}"}


async def main() -> None:
    """Read commands from stdin until ``quit`` or EOF."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="ws://127.0.0.1:9507/ws")
    parser.add_argument("--room", required=True)
    parser.add_argument("--name", default="py")
    args = parser.parse_args()
    rec = Recorder()
    client = LoadClient(args.name, args.room, args.url, rec, seed=1)
    seen: dict[str, float] = {}
    watcher = asyncio.create_task(watch_presence(client, seen))
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while line := await reader.readline():
        cmd = json.loads(line)
        try:
            reply = await handle(client, rec, cmd, seen)
        except Exception as exc:  # noqa: BLE001 - reported to the driver
            reply = {"error": repr(exc)}
        print(json.dumps({"cmd": cmd["cmd"], **reply}, default=str), flush=True)
        if cmd["cmd"] == "quit":
            break
    watcher.cancel()


if __name__ == "__main__":
    asyncio.run(main())
