# Phase 1 exit measurements (Step 1.22)

**Result: three of four criteria met; autocomplete misses its target.** The 1M-record bootstrap
ran on real LUX data in 21.8 min. Publish-to-QLever p95 is **0.27 s** under 20 publishes per
second. Three editors co-edited one record for a minute and lost nothing. Autocomplete p95 at 1M
records is **211-246 ms** for the guide's 200 random prefixes, against a target of 100 ms. That is
after a query-plan fix in this step that changes no result; it was 568 ms before. No minimum
prefix length gets it under 100 ms with the current QLever query. The options are measured in §4.3;
choosing one changes behaviour, so it is the owner's decision (`docs/questions.md`, Step 1.22).

| Exit criterion (`docs/implementation_phases.md`, Phase 1) | Measured | |
| :-- | :-- | :-- |
| 1M-record bulk load completed and timed | 1,000,000 LUX records, 999,994 loaded; aat-labels + load + export-nt + build-index **1,307 s (21.8 min)** | **met** |
| Publish-to-QLever p95 under 1 s | **0.271 s** (outbox commit to QLever acknowledgement, 2,400 publishes at 20/s); a QLever query sees the edit **0.271 s** (p95) after the publish answers | **met** |
| Three concurrent editors, no data loss | 60 s, three browsers, about 580 overlapping edits; the published JSON-LD has every edit and passes the round-trip invariant (4 of 4 runs) | **met** |
| Autocomplete p95 under 100 ms on the loaded index | 200 random prefixes: **p95 211 ms** (246 ms after the storm). By length: 2 characters 264 ms, 3 characters 184 ms, 4 to 8 characters 151 ms | **not met** |

| | |
| :-- | :-- |
| Date | 2026-11-23 |
| Machine | Apple M5 Max, 18 cores, 128 GB, internal SSD, macOS 26.6.2. Shared with other agents: the 1-minute load average was 54-68 during the bulk load and 3-11 during every other measurement. |
| Software | `main` at `d24e137` plus this step; Python 3.12 (uv); PostgreSQL 18 (Postgres.app, stock settings); QLever 0.6.0 (Homebrew) with `deploy/Qleverfile` |
| Stack | One process each, as in `deploy/Procfile`: `lae api` (port 9035), `lae crdt-ws` (9535), `lae sync-worker` (`/metrics` on 9835), `qlever-server` (7035, `PERSIST_UPDATES`, access token). Database `lae_i35`. `LAE_DEV_USER` with the Editor role instead of a login. |
| Scripts | `scripts/load/bootstrap_1m.sh` (with `scripts/load/sample_lux.py`), `scripts/load/publish_storm.py`, `scripts/load/autocomplete_latency.py`, `frontend/e2e/collab.spec.ts` (with `scripts/check_roundtrip.py`) |

These measurements run locally only. They need a 1M load and QLever, and neither may run in CI
. The collaboration test is part of the Playwright suite and does run in CI.

## 1. Bulk bootstrap at 1M records

Details, per-stage throughput and the 10M projection (about 3.7-3.8 h): `docs/spikes/S6-bootstrap.md`,
"Phase 1 gate run (Step 1.22)".

- **Source: the real LUX dump.** All 24 files (44,017,795 records) were present and decompressed
  to the end, so the generator was not used. The sample is the 1M records with the smallest keyed
  BLAKE2b-64 of their `id`. That choice is deterministic and does not depend on file order. Every
  record type appears in proportion to the dump: 35 % LinguisticObject, 34 % HumanMadeObject,
  12 % Person, 12 % DigitalObject, then Group, Place, Type, VisualItem, Set, Activity and the rest.
- **Stages:** input sample 65.7 s; `aat-labels` 1.7 s; `load` 873.8 s (999,994 written, 6
  rejected, 804,190 flagged); `export-nt` 248.9 s (70,708,377 triples); `build-index` 182.6 s
  (70,825,975 distinct triples, peak RSS 4.26 GiB, 2.98 GB index).

## 2. Publish storm: publish to QLever under load

`scripts/load/publish_storm.py --rate 20 --duration 120` against the running stack, straight after
the bootstrap. Each publish does what an editor does:

1. `GET /api/records/{id}` for the draft epoch.
2. A y-sync WebSocket to `crdt-ws`.
3. One Yjs `Text` splice on the first `Name` (through `lae.projection.ops`).
4. A sync round trip, so the update is persisted.
5. `POST /api/records/{id}/publish`. On 409 `LAE-PUB-002` it accepts the reported warnings and
   sends the publish once more.

Publishes start open loop, every 50 ms whether or not the earlier ones have finished. The records
are 1,200 bulk-loaded `HumanMadeObject` / `Person` / `Place` / `Type` records with no open QA
finding. Each one is published twice, 60 s apart. So 1,190 publishes were the first edit of a
bulk-loaded version, and the rest were ordinary second revisions. Ten records had been published
by a smoke run of the script before the storm.

| | |
| :-- | --: |
| Publishes started / succeeded | 2,400 / **2,400** (8 of them after accepting warnings; no failure of any kind) |
| Achieved rate | 20.0 publishes/s over 119.97 s |
| Outbox events | 2,400, all `done` (none pending or failed); 0 with `drift_check` |
| Outbox drained | 0.31 s after the last publish answered |
| **Publish to QLever** (`processed_at - created_at` per event) | p50 0.157 s, **p95 0.271 s**, p99 0.285 s, max 0.344 s |
| **Visible in QLever** (480 probes: `ASK` for the new name, polled every 25 ms from the publish answer) | p50 0.152 s, **p95 0.271 s**, p99 0.285 s, max 0.290 s; no timeout |
| **Sync lag** (`sync_lag_seconds` from the worker's `/metrics`, 1,187 samples every 100 ms) | p50 0.120 s, **p95 0.257 s**, max 0.304 s; `outbox_pending` never above 1 in a sample |
| SPARQL updates sent | 532 for 2,400 events (micro-batching, `BATCH_WINDOW_S = 0.25`) |
| **QLever delta triples** (`qlever_delta_triples`, QLever `/metrics`) | 50 before, **6,010 after** (+5,960) |
| Publish HTTP request | p50 19 ms, p95 52 ms, p99 92 ms |
| Whole publish (epoch, WebSocket, edit, publish) | p50 28 ms, p95 78 ms |

The latency is set by the worker's 0.25 s batching window, not by load. A smoke run at 2
publishes per second gave p95 0.31 s. `created_at` is the start of the publish transaction, so
the outbox figure slightly overstates the time from commit to QLever.

## 3. Three-editor collaboration

`frontend/e2e/collab.spec.ts` runs against the real `lae api`, `crdt-ws` and production build
(`playwright.config.ts`). Three browser contexts open one new `HumanMadeObject`, which has a
title and one dimension (Height, 10 cm). For 60 s they edit at the same time:

- **A** types a word at the end of the title, key by key.
- **B** switches the material "canvas" on and off.
- **C** types a new dimension value. Every third time it also switches "oil paint", so B and C
  write the same `made_of` array concurrently.

When the time is up, the test does three things:

1. It waits until all three editors show the same title, value and materials.
2. A publishes through the Publish dialog.
3. The test checks that `/id/{uuid}` (JSON-LD) has every one of A's words in order, C's last
   value, the unit and type unchanged, and exactly the materials that B's and C's last toggles
   leave on. `scripts/check_roundtrip.py` (the oracle of `backend/tests/test_roundtrip.py`) then
   confirms that `hydrate` → `project` → `strip_internal_iris` gives the published record back.

| Run | Title words (A) | Canvas toggles (B) | Values (C) | Oil-paint toggles (C) | Result |
| :-- | --: | --: | --: | --: | :-- |
| 1 | not logged | | | | passed (1.1 min) |
| 2, 3 (two at once) | 210 each | 134 each | 180 each | 60 each | both passed |
| 4 (whole e2e suite, in parallel) | 224 | 139 | 186 | 62 | passed |

No edit was lost. axe reports no violation on the editor with three users present.

**Extra probe, not committed:** two editors typed into the *same* title as fast as Playwright
types, one at the start and one at the end, for 30 s. They typed 1,042 and 1,044 words, about
10,300 characters. That is roughly 170 one-character updates per second into one room. Both
editors converged on exactly the expected text, but only **8.4 s** after the typing stopped. In a
run that compared the two views after 2 s, they still differed while the server's persisted draft
already held all 9,218 characters. So the server lost nothing and the broadcasts lagged. `crdt-ws`
persists each update before it reads the next frame from that socket, and at this rate (far above
human typing) the queue builds up. This is not a data-loss bug. It would matter for agents that
write many small updates (Phase 3).

**Follow-up (core review slice A, #178, 2026-11-25).** Reproduced without a browser: two
`pycrdt` clients over real WebSockets to a real `crdt-ws` and PostgreSQL, each inserting one
character at a time into the same `Text`, 170 updates per second in total.

| Measurement | Result |
| :-- | :-- |
| 170/s for 10 s (1,700 updates) | server and both clients complete and identical 3.6 s after the typing stopped |
| 170/s for 30 s (5,100 updates) | the same after 4.7 s |
| 80/s for 15 s, 20/s for 15 s | no lag (complete within the 10 ms poll) |
| `Room.write`, 1,000 one-character updates in sequence, no sockets | 5.7 to 6.0 ms each: 168 to 177 per second |
| of which `store.append_update` and its commit | 4.6 to 5.1 ms |
| the same statements, 50 updates per transaction | 2.9 to 3.9 ms per update |

The clients were complete at the moment the server was, so the broadcast adds nothing; the
limit is the write path. A room persists one update per transaction (four round trips to
PostgreSQL and a durable commit), one at a time under the room lock, before it applies and
broadcasts it (ADR-0027 invariant 2). That gives one room about 170 to 200 updates per second
on this machine, which is the rate the probe typed at; in a browser, rendering each remote
update adds to it (in the run above the server held everything after 2 s and the views still
differed). Updates beyond the limit wait in the sockets, in order, under TCP back-pressure, and
nothing is dropped. It is a throughput limit, not a defect: three people typing fast into one
field produce about 30 updates per second. #179 proposes writing the frames that are
waiting in one transaction, for Phase 3.

## 4. Autocomplete latency

`scripts/load/autocomplete_latency.py` sends `GET /api/search/autocomplete?q=…&types=…` to the
running `lae api`, one request at a time. It measures the time to the full answer at the client.

- **Prefixes:** each comes from a deterministic random `entity_labels` row of the requested
  classes (seed 20261123). The script takes a random word of the row's label and cuts it to the
  set's length.
- **Classes:** `types` rotates through what the UI profiles ask for: `Person,Group`, `Place`,
  `Type`.
- **Sets:** four, each of 200 distinct prefixes (5 warm-up requests not counted):
  - `2` and `3`: exactly two and three characters;
  - `4+`: four to eight characters;
  - `mix`: lengths uniform in 2-6, the guide's "200 random prefixes".
- **Cache:** QLever's cache is cleared before each set, and no prefix repeats within a set, so
  repeats do not flatter the numbers.

The index holds 144,767 Person/Group, 13,966 Place and 70,202 Type rows in `entity_labels` (AAT
included).

### 4.1 Results (milliseconds)

| Set | Before (as on `main`) p50 / p95 / p99 | With the query-plan fix p50 / **p95** / p99 | After the storm (fix) p50 / **p95** / p99 |
| :-- | :-- | :-- | :-- |
| 2 characters | 389 / 632 / 848 | 131 / **264** / 377 | 134 / **281** / 386 |
| 3 characters | 320 / 392 / 449 | 131 / **184** / 223 | 140 / **177** / 239 |
| 4-8 characters | 186 / 341 / 356 | 100 / **151** / 174 | 108 / **153** / 169 |
| mix (2-6) | 320 / 568 / 776 | 122 / **211** / 399 | 126 / **246** / 292 |

By class (p95, mix, with the fix): Person,Group 168 ms, Place 130 ms, Type 389 ms. No request
failed. "After the storm" ran with the 1,200 published records in the overlay (`entity_labels`
rows newer than the index).

Before the storm, no answer came back empty. After it, two did: `storm` and `storm1` (Place).
Both were drawn from labels the storm had just written, such as `Lyon ~storm1234`. The
`entity_labels` row is newer than the index, so only the overlay can find it. The overlay
matches a word only after a space (`ILIKE '% storm%'`), while QLever's text index starts a word
after any character that is not a letter or digit, here `~`. A word that follows punctuation is
therefore found once the index is rebuilt, but not before. This is a small inconsistency between
the two sources, not a latency problem. It is left for the Step 2.18 rebuild work.

### 4.2 Where the time goes, and the fix in this step

Measured per part on 50 prefixes of the `mix` set, QLever cache cleared:

| Part | p50 | p95 |
| :-- | --: | --: |
| QLever prefix search | 320 ms | 396 ms |
| QLever context query (life dates, parents) | 20 ms | 23 ms |
| Postgres overlay (`entity_labels` newer than the index) | 0.3 ms | 0.4 ms |

QLever's query plan for `q=Jo&types=Person,Group` (432 ms) breaks down as follows:

- The text index answers in 4 ms (103k matches).
- The two `?entity rdf:type ?class` joins scan 16.6M `rdf:type` triples each (75 ms each).
- The joins to `rdfs:label` and to `Name` content take about 100 ms.
- The `/node/` regex filter and the rank `BIND` run over 30k rows (90 ms) before `GROUP BY`.

**Fix (separate commit, `perf(search)`): one `rdf:type` pattern per class.** For up to three
classes, `build_search_query` now writes `{ ?entity rdf:type <class> . BIND(<class> AS ?class) }`
per class in a `UNION`, instead of `?entity rdf:type ?class` plus `FILTER(?class IN (…))`. QLever
then reads only those classes' `rdf:type` triples. For more classes (a request without `types`)
it keeps the old form, which is faster there: with all record classes the per-class form failed
the 100 ms test in `tests/search/test_autocomplete_qlever.py`.

Checks on QLever directly, old and new query in turn for each prefix, cache cleared each time:

- **100 prefixes (the `2` and `mix` sets):** p50 330 → 166 ms, p95 566 → 370 ms.
- **50 prefixes:** identical `?entity`, `?type` and `?rank` rows. Only `SAMPLE(?lit) AS ?label`
  may pick another of an entity's matching literals. SPARQL leaves that choice open, and the API
  shows the `entity_labels` label whenever the entity has a row, which every loaded entity has.

The search tests, including the QLever-backed ones run locally (`LAE_TEST_QLEVER_PORT`), pass.

### 4.3 Options for the owner (behaviour changes; none implemented)

| Option | Effect on behaviour | p95, measured here |
| :-- | :-- | :-- |
| A. Keep as is | none | 211 ms (mix), 264 ms (2 characters) |
| B. Minimum prefix 3 characters | 2-character queries refused or answered empty | 184 ms (3 characters), 151 ms (4+): **still over 100 ms** |
| C. Minimum prefix 4 characters | as B, for 3 characters too | 151-153 ms: **still over 100 ms** |
| D. Rely on QLever's cache for repeated prefixes | none | No gain over a pass of 200 prefixes: a second identical pass gave p95 236 ms (first 222 ms), because the 2 GB cache does not keep them (cached sub-results of the large scans evict them). An immediate repeat of one query answers in about 35 ms. |
| E. Prefix search in Postgres (`entity_labels`, expression index `left(lower(default_label), 64) text_pattern_ops`) instead of QLever | Matches only the **start of the default label**, not a word inside it, another `Name`, or another language's label. Of each 200-prefix set, 7-30 prefixes found nothing this way that QLever does find. | SQL only (no HTTP, no context query): 2 characters 191 ms; 3 characters **11 ms**; 4+ **1.7 ms**; mix 46 ms |
| F. E for 3+ characters with QLever kept for whole-word matches, or E plus a 3-character minimum | as E, or E plus B | about 11 ms (SQL) + about 20 ms (context query) + HTTP |
| G. A newer QLever | none | not measured. Spike S1 found master `ba7b74c` 2.2x faster at index building. Query speed on this shape is unknown. |

A result cap does not help: the cost is in the joins and the `GROUP BY` over every match, before
`LIMIT` (the UI already asks for 20). Making the text match cheaper means changing what matches
(E, F) or the engine (G).

The index for option E was created on `lae_i35` only for the measurement and dropped again.

## 5. Reproducing

From the repository root, with `LAE_DATABASE_URL` pointing at an empty migrated database and
`LAE_QLEVER_ACCESS_TOKEN` set:

```
WORK=/tmp/lae-i35/1m INDEX_NAME=lae_i35 QLEVER_PORT=7035 LAE_BASE_URI=http://127.0.0.1:9035/id/ \
  AAT_CSV=../linkedart-parser/data/aat_dataset.csv scripts/load/bootstrap_1m.sh
# start api (9035), crdt-ws (9535), sync-worker (LAE_SYNC_METRICS_PORT=9835) on that database,
# with LAE_QLEVER_URL / LAE_QLEVER_UPDATE_URL on QLever and LAE_DEV_USER=<email>:Editor
uv run --project backend python scripts/load/autocomplete_latency.py --api http://127.0.0.1:9035 \
  --qlever http://localhost:7035 --qlever-token "$LAE_QLEVER_ACCESS_TOKEN" --database-url "$LAE_DATABASE_URL"
uv run --project backend python scripts/load/publish_storm.py --api http://127.0.0.1:9035 \
  --crdt-ws ws://127.0.0.1:9535 --metrics http://127.0.0.1:9835/metrics --qlever http://localhost:7035 \
  --qlever-token "$LAE_QLEVER_ACCESS_TOKEN" --database-url "$LAE_DATABASE_URL" --out /tmp/storm.json
cd frontend && corepack pnpm build && corepack pnpm exec playwright test e2e/collab.spec.ts
```

Stop the QLever server with the exact command `build-index` prints (`qlever stop
--cmdline-regex` on the index name and port). Never run a bare `qlever stop`.
