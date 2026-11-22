# Spike S2: pycrdt-websocket under load

**Result: GO.** `pycrdt-websocket==0.16.9` on `pycrdt==0.14.13` (the pinned pair) carried 30 editors in 10 rooms for 5 minutes, with disconnect/reconnect churn, and ended with no divergent or lost state. Every expected awareness update arrived, live updates propagated with a p95 of 5.1 ms (7.7 ms with a synchronous Postgres insert before each broadcast), and a real Yjs + `y-websocket` client in headless Chromium converged with Python clients and the server. Five room-lifecycle and presence gaps (findings 2–5 and 8 under "Findings for Step 1.8") have to be closed inside `lae.crdt.rooms.RoomManager`. None of them needs a fork or the fallback.

Phase 0, Step 0.7 (issue #7). Measured on 2026-11-22.

## Setup

| Item | Value |
| :-- | :-- |
| Machine | Apple M5 Max, 128 GB, macOS 26.6.2. Server, clients and browser all on one host over loopback (no network latency, no TLS). |
| Server | `scripts/spikes/s2_crdt/server.py`: FastAPI 0.165.0 (Starlette 1.10.0) mounting `pycrdt.websocket.ASGIServer` at `/ws`. One `WebsocketServer` with `auto_clean_rooms=False` holds the rooms in memory, one room per path (`/ws/<room>`). It runs in a single uvicorn 0.54.0 process with one event loop. `ws="auto"` selects `websockets-sansio`. Python 3.12.11. |
| Python clients | `scripts/spikes/s2_crdt/client_load.py`: `pycrdt` `Doc` + `Awareness` over `websockets` 17.4 (`websockets.asyncio.client`). The client speaks y-sync and awareness itself and does not echo remote updates. All 30 clients share one asyncio process. Client event-loop lag p99 was 3.7 ms or less in every run, so the clients were not the bottleneck. |
| Browser client | `scripts/spikes/s2_crdt/browser/` (`index.html` and a ~50-line `main.js`) uses `yjs` 13.6.33 and `y-websocket` 3.1.0 from `frontend/node_modules`. Vite 8.3.8 serves it on port 9007. `browser_check.mjs` drives Chromium 159 (Playwright 1.65.1 headless shell) and a pycrdt peer (`browser_peer.py`). Nothing was added to `frontend/src`. |
| Persistence variant | `server.py --persist-dsn postgresql://localhost/lae_i7` inserts every incoming document update into Postgres 18 (Postgres.app, local) from `YRoom.on_message` before the room applies and broadcasts it. |
| Ports and data | crdt-ws 9507, Vite 9007, database `lae_i7` (dropped afterwards), scratch files in `/tmp/lae-i7/`. These are the slot-7 resources. |

### Scenario (guide Step 0.7, items 2 and 3)

- 30 clients, 3 per room, in 10 rooms, for 300 s.
- Each client makes one `Y.Map` set and one `Y.Text` insert per second, as two separate transactions. Awareness is updated every 2 s.
- The map value is `{seq, t, online, by}`. The text insert is a token `[c07.123@<epoch ms>o]` placed at a random token boundary, so tokens never split and can be counted at the end. The writer's wall-clock timestamp travels inside every payload. Receivers compute latency against the same clock.
- Churn: every 30 s, 5 clients disconnect, each from a different room, rotating through the rooms and through the clients in each room. Each round alternates graceful closes (awareness null-state, then a close frame) with abrupt TCP aborts. The disconnected clients stay offline for 5 s and keep editing, then reconnect with the same `Doc`. 9 rounds gave 45 disconnects (27 graceful, 18 abrupt).
- End of run: a 5 s settle, then every client and the server room are compared. The checks are: decoded state vectors, a SHA-256 of the projected content, every written text token present exactly once, and every client's last map value present everywhere.
- **Live** latency counts only updates that travelled as a y-sync `update` while the writer and the receiver were both connected. Updates written offline, or received during a resync, are reported as **catch-up**.

## Results: load (30 clients, 10 rooms, 5 min, churn)

Run 1 is the headline run and uses the committed scripts. Run 2 adds a synchronous Postgres insert of every update before it is broadcast. The repeat is an earlier identical-scenario run, made with a pre-release revision of `client_load.py` that differs only in reporting. Raw results are in `scripts/spikes/s2_crdt/results/load-*.json`.

| Measure | Run 1 (in-memory) | Run 2 (persist before broadcast) | Repeat (in-memory) |
| :-- | :-- | :-- | :-- |
| Edits written | 9,000 `Y.Map` sets and 9,000 `Y.Text` inserts | same | same |
| Messages sent / received by the clients | 22,095 / 65,862 (5.2 MB received) | 22,095 / 65,863 | 22,095 / 65,863 |
| **Live update propagation** p50 / p95 / p99 / max (n = 34,200 deliveries) | 2.0 / **5.1** / 9.6 / 28.1 ms | 2.9 / **7.7** / 10.0 / 14.5 ms | 3.0 / **7.3** / 8.7 / 12.5 ms |
| `Y.Map` set p95, `Y.Text` insert p95 | 4.6 ms, 5.5 ms | 6.2 ms, 8.9 ms | 6.8 ms, 7.7 ms |
| **Awareness propagation** p50 / p95 / max (n = 8,718) | 0.8 / 3.8 / 15.3 ms | 0.8 / 3.0 / 5.6 ms | 1.3 / 5.7 / 8.5 ms |
| **Awareness delivery** (sender and receiver both connected) | 8,610 of 8,610 (100%) | 8,610 of 8,610 | 8,610 of 8,610 |
| **Divergence**: rooms whose 3 clients and server room have equal decoded state vectors and content digests | 10 of 10 | 10 of 10 | 10 of 10 |
| Text inserts lost or duplicated | 0 of 9,000 | 0 | 0 |
| Each client's last `Y.Map` value present in every replica | yes, all 10 rooms | yes | yes |
| **Reconnects** (27 graceful, 18 abrupt) | 45, all synced | 45 | 45 |
| Reconnect: socket open, p50 / max | 1.6 / 4.8 ms | 1.6 / 2.2 ms | 1.9 / 3.6 ms |
| Reconnect: open to sync step 2 received, p50 / max | 2.8 / 8.2 ms | 2.5 / 3.6 ms | 3.2 / 6.0 ms |
| Sync step 2 sent to a reconnecting client, p50 / max | 2.1 / 3.2 KB | same | same |
| Catch-up: updates that could not travel live (written offline, or missed while offline; 1,080 deliveries), delay after the later of the two reconnects, p50 / p95 / max | 1.3 / 4.0 / 4.4 ms | 1.4 / 2.6 / 4.6 ms | 1.5 / 2.5 / 3.0 ms |
| Graceful leave: presence removed at peers, p50 / max (n = 54) | 0.5 / 1.7 ms | 0.5 / 2.7 ms | 0.5 / 1.8 ms |
| Abrupt leave: presence removed during the 5 s gap | 0 of 18 (ghost kept, see finding 5) | 0 of 18 | 0 of 18 |
| Initial connect of 30 clients to synced, p50 / max | 7.9 / 9.4 ms | 9.7 / 11.9 ms | 7.3 / 9.0 ms |
| **Server RSS**: baseline, after connect, end of run | 55.9, 59.7, 67.2 MB | 62.9, 66.5, 74.7 MB (asyncpg loaded) | 55.6, 59.4, 67.0 MB |
| **Server RSS per room**: (end − baseline) / 10 | **1.13 MB** | 1.18 MB | 1.14 MB |
| RSS per room right after connect (empty documents, 3 sockets) | 0.38 MB | 0.36 MB | 0.38 MB |
| Encoded document per room at end | 56 KB (23 KB text) | 56 KB | 56 KB |
| Server CPU during the 300 s | 2.4 s (0.8% of one core) | 3.6 s (1.2%) | 3.7 s (1.2%) |
| Server event-loop lag (50 ms timer) p99 / max | 2.8 / 10.7 ms | 1.7 / 3.0 ms | 1.7 / 6.0 ms |
| Postgres insert per update, p50 / p95 / p99 / max (n = 17,625; 1.26 MB) | n/a | 1.0 / 3.6 / 4.9 / 36.6 ms | n/a |
| Server exceptions / logged errors / client errors | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 4 / 0 (finding 6) |
| Clients left in rooms after everyone disconnected; rooms after evict-all | 0; 0 | 0; 0 | 0; 0 |

RSS grew steadily during the run because every document grows. After 1,800 updates a room's document is 56 KB encoded, with a 23 KB text and 900 tombstoned map values. Growth stopped when the edits stopped. The memory probe below shows that evicted rooms are freed.

## Results: memory per resident room

`probes.py rooms` opens 200 rooms, one client each. Each client holds a synthetic `HumanMadeObject` that is naively hydrated: objects become `Y.Map`, arrays become `Y.Array`, and `content` becomes `Y.Text`. This is a size proxy, not the Step 0.8 projection. Each row uses a fresh server.

| Record (JSON) | Y.Doc encoded | RSS per room, client connected | RSS per room, resident without client | 200 rooms opened and synced in |
| :-- | --: | --: | --: | --: |
| 0.6 KB (`--parts 0`) | 0.7 KB | 150 KB | 153 KB | 0.09 s |
| 5.9 KB (`--parts 4`) | 7.9 KB | 258 KB | 261 KB | 0.14 s |
| 22.1 KB (`--parts 16`) | 29.6 KB | 591 KB | 594 KB | 0.29 s |

Fixed overhead per room (the `YRoom`, its anyio task group and streams, the `Awareness`, and an empty `Doc`) is about 150 KB. Each further KB of record JSON costs about 20 KB of RSS when hydrated naively into nested `Y.Map`s.

- Connections are cheap. RSS with the 200 clients connected is the same as with the rooms resident and no clients.
- After eviction, `gc` finds **0** live `YRoom`, `Doc` and `Awareness` objects, so there is no leak. RSS stays at its high-water mark because the allocator keeps the pages. A second cycle of 200 rooms on the same server added only 0.16 MB, so that memory is reused.
- For the spec's 10–30 users, 100 resident rooms of 20 KB records is about 60 MB. Memory is not a constraint. Idle eviction (B5) keeps the resident set bounded.

## Results: browser wire compatibility (Chromium + Yjs + y-websocket ↔ pycrdt-websocket ↔ pycrdt)

`node scripts/spikes/s2_crdt/browser_check.mjs`: all 15 checks pass (`all_ok: true`).

| Check | Result |
| :-- | :-- |
| Python peer writes first, then the browser joins | Synced 70 ms after navigation started. The peer's 3 map values and 3 text tokens are present. |
| Browser presence at the Python peer (y-websocket announces on open) | Seen 62 ms after the join |
| Idle Python peer's presence at the browser | Seen **14.6 s** after the join, only at the peer's 15 s renewal (finding 4) |
| Live awareness, Python to browser (10 updates at 100 ms) | 10 of 10, p50 / p95 0.3 / 0.4 ms |
| Concurrent live edits: 40 `Y.Map` sets and 40 `Y.Text` inserts from each side | Python to browser p95 / max 0.4 / 0.9 ms. Browser to Python p95 / max 1.4 / 1.6 ms (n = 80). |
| Browser offline (`provider.disconnect()`), 5 edits on each side, then `connect()` | Resynced in 12 ms. The browser's offline edits were already at the Python peer when the browser reported synced. |
| **Final state**: browser, Python peer and server room | Decoded state vectors equal, `Y.Map` JSON equal, `Y.Text` equal (2,439 chars, 93 tokens: 48 Python and 45 browser, none lost or duplicated) |
| Graceful Python leave (awareness null-state) | Removed in the browser within the same millisecond |
| Abrupt Python leave (TCP abort) | Ghost presence in the browser for **31.3 s** (y-protocols 30 s timeout) |
| Chromium tab close | Ghost presence at the Python peer for **32.5 s** (pycrdt `Awareness` 30 s timeout) |
| Browser console errors | None |
| Types pycrdt reads from browser writes | `seq` (a JS integer) as `float`, `t` as `float`, `online` as `bool`, `by` as `str` |

`Date.now()` has 1 ms resolution, so sub-millisecond browser latencies can show as slightly negative.

## Results: room eviction (`probes.py eviction`)

| Scenario | Observed |
| :-- | :-- |
| `delete_room` while 2 clients are connected | The room stops, but **both sockets stay open**. Updates from one no longer reach the other (not delivered within 3 s). A newcomer on the same path gets a **new, empty room** and never sees the old state. The zombie peers never see the newcomer. This is a silent partition. |
| Everyone leaves, room evicted, one client reconnects | The new room is re-seeded by the reconnecting client's sync step 2. A fresh client then receives the full state, and the server digest equals the client's digest. |

## Findings for Step 1.8 (RoomManager / `crdt-ws`)

These are facts measured in this spike, or read in the pinned sources (marked *source*). They are inputs for the critical Step 1.8, not decisions.

1. **Import path.** The guide says `pycrdt_websocket.ASGIServer`. From 0.16 the distribution `pycrdt-websocket` installs the namespace package `pycrdt.websocket` (`from pycrdt.websocket import ASGIServer, WebsocketServer, YRoom`).
2. **Rooms vanish by default.** `WebsocketServer(auto_clean_rooms=True)`, the default, deletes a room and its in-memory `Doc` as soon as its last client leaves. S2 used `False`. The RoomManager must own eviction (B5: compact, then drop after 10 minutes idle).
3. **Never drop a room that has sockets.** `delete_room()` stops the room but does not close its clients (see the eviction table above). Every path that drops or replaces a room must close that room's sockets first, with an explicit close code. That covers publish and close, discard, `eject(user_id)`, and the stale-epoch guard. Idle eviction must only take rooms with zero clients.
4. **No awareness snapshot on join.** *source and measured:* `YRoom.serve` sends only sync step 1 to a joining client. It does not send the awareness states the room already holds, so a joiner sees idle co-editors only at their next 15 s renewal (14.6 s measured in Chromium). The reference y-websocket server sends all states on connect. Fix in a `YRoom` subclass: on connect, send `room.awareness.encode_awareness_update(list(room.awareness.states))`.
5. **Ghost presence after abrupt leaves and tab closes.** *source and measured:* pycrdt-websocket does not clear the awareness states a closed socket controlled. y-websocket 3.1 in a browser sends no null-state on tab close or `provider.disconnect()`; it only does so on Node `process.exit`. Peers therefore keep a ghost until the 30 s outdated timeout: 31.3 s measured in the browser after a pycrdt peer's TCP abort, and 32.5 s measured at a pycrdt peer after a Chromium tab close. Graceful null-state leaves propagate in about 1 ms. Fix on the server: track the awareness client IDs seen on each channel and broadcast null states for them when the channel closes, as the reference server does. A frontend `pagehide` handler that sets the local state to `null` also helps.
6. **The server is an awareness peer.** *source and measured:* the room's own `Awareness` has local state `{}` and broadcasts its renewal to every client every 15 s. Clients see one extra, empty state and should ignore states without `user`. This broadcast caused the only server log errors in the three runs: 4 × `Error while broadcasting awareness changes … ClientDisconnected` in the repeat run, when it raced the first churn round's disconnects. They are logged and swallowed, and nothing was lost.
7. **Persist-before-broadcast fits without a fork.** *source and measured:* `YRoom.serve` awaits `room.on_message(message)` before it applies or broadcasts a message, so B5's "persist to `crdt_updates` before broadcast" can be done in that hook. In the persistence run the insert took 1.0 ms p50 and 3.6 ms p95 per update, and end-to-end live p95 was 7.7 ms (5.1 ms in-memory). The hook gets only the raw message, so the room id has to be bound when the room is created (as `server.py` does in `SpikeWebsocketServer.get_room`).
8. **Rejecting a connection.** *source:* `ASGIServer.on_connect` returning `True` refuses the handshake (HTTP 403). It cannot send a WebSocket close code. B6's `4409 stale-epoch` needs the server to accept the socket and then close it with 4409. y-websocket 3.1's default `shouldReconnect` treats 4400–4499 as terminal (no reconnect loop), which matches B6.
9. **Numbers come back as floats.** *measured:* pycrdt returns every JSON number as a Python `float`. That applies to ints set from Python and to JS integers written by the browser (`seq` reads as `float`). This matters for Step 0.8's canonical-JSON round trip: an integer such as `"value": 12` must not project as `12.0`.
10. **Compare state vectors decoded.** yrs encodes state-vector entries in hash-map order and Yjs sorts them, so equal vectors can differ byte-wise. `s2common.decode_state_vector` decodes them to `{client: clock}`.

## GO / NO-GO

**GO.** The pinned `pycrdt` + `pycrdt-websocket` pair meets every S2 criterion at the spec's upper bound of 30 concurrent editors:

- zero divergence or loss;
- 100% awareness delivery;
- single-digit-millisecond p95 propagation;
- clean reconnects with offline edits merged both ways;
- about 1% of one core;
- wire compatibility with Yjs 13.6 + y-websocket 3.1 in Chromium.

Findings 2–5 and 8 are room-lifecycle and presence behaviours that the `lae.crdt.rooms.RoomManager` / `YRoom` subclass in Step 1.8 must implement. They are small, local hooks, and none of them requires replacing the server.

The fallback named by the guide and `docs/implementation_phases.md` is a Yjs-wire-compatible sidecar (y-websocket server or Hocuspocus) behind `lae.crdt.rooms.RoomManager`. It is **not** triggered and remains the contingency.

## How to reproduce

From `backend/`, with the spike dependency group (`psutil`, `websockets`):

```
uv run --group spike python ../scripts/spikes/s2_crdt/server.py --port 9507 &
uv run --group spike python ../scripts/spikes/s2_crdt/client_load.py --out /tmp/lae-i7/s2-load.json
uv run --group spike python ../scripts/spikes/s2_crdt/probes.py rooms --count 200 --parts 4   # fresh server per size
uv run --group spike python ../scripts/spikes/s2_crdt/probes.py eviction
(cd .. && node scripts/spikes/s2_crdt/browser_check.mjs --out /tmp/lae-i7/s2-browser.json)
# persistence variant: restart the server with --persist-dsn postgresql://localhost/lae_i7
```

`browser_check.mjs` needs `corepack pnpm install` in `frontend/` and `corepack pnpm exec playwright install --only-shell chromium`. The raw results of the runs reported here are in `scripts/spikes/s2_crdt/results/`.
