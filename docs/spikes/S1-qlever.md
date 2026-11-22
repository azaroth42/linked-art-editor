# Spike S1: QLever updates, persistence, text freshness, SERVICE, rebuild timing

**Result: NO-GO for text freshness; GO for everything else.** SPARQL Update works, is fast, and
survives a restart and a `kill -9`. `SERVICE` receives the pushed-down `VALUES`. A second index
builds from scratch while the first keeps serving. But on the pinned binary (Homebrew QLever 0.6.0)
neither `ql:contains-word "zebra*"` nor `ql:has-word "zebra"` finds a literal inserted by SPARQL
Update until the index is rebuilt. A build of current upstream master fixes whole-word
`ql:has-word`, but not prefix search. The fallback named in `docs/implementation_phases.md` applies:
autocomplete unions its QLever results with the `entity_labels` overlay (Phase 1 Step 1.13).
`docs/questions.md` (Step 0.6) asks for the acknowledgement that decision F9 requires.

| | |
| :-- | :-- |
| Step | Phase 0, Step 0.6 (issue #6) |
| Date | 2026-11-22 |
| Machine | Apple M5 Max, 18 cores, 128 GiB, macOS (Darwin 25.6, arm64), internal SSD. Shared with other agents: load average 3.5 to 17 during the runs, so latencies vary by up to 40 % between runs (section 9). |
| Binary **A** (pinned) | Homebrew `qlever` 0.6.0_1: `qlever-index` / `qlever-server` v0.6.0, git `6c5861a` (as in `deploy/QLEVER_VERSION`). **Every number is from A unless marked B.** |
| Binary **B** (comparison) | `lux/qlever-dev` at `ba7b74c` (upstream master, 480 commits after A), built from source out of tree, Release (section 8). Reports itself as `0.0.0-unknown` because the build directory is outside git. |
| Configuration | `deploy/Qleverfile` unchanged except `ENABLE_METRICS = true` (added by this step): `TEXT_INDEX = from_literals`, `ADD_HAS_WORD_TRIPLES = true`, `STXXL_MEMORY = 4G`, `PERSIST_UPDATES = true`, `-m 4G -c 2G -j 4`. Started and stopped with `make up` / `make down` on slot 6 (`QLEVER_PORT=7006`, green 7306, stub 9806). |
| Scripts | `scripts/spikes/s1_qlever/` (`run_s1.sh` runs steps 2–7 for one binary; section 10) |
| Raw results | `docs/spikes/s1-qlever-results/` (JSON per step and binary) |

## Summary

| Q | Question (guide step) | A: Homebrew 0.6.0 (`6c5861a`) | B: master (`ba7b74c`) | Verdict |
| :-- | :-- | :-- | :-- | :-- |
| 2 | Build a 10M-triple index natively | 13.1 s, peak RSS 1.3 GiB | 7.1 s, 1.4 GiB | measured |
| 3 | `INSERT DATA` / `DELETE DATA` (100 triples), `DELETE WHERE` (1 subject), 1,000 small updates | p50 3.4 / 3.2 / 3.1 ms; small updates p50 2.85 ms, p95 3.90 ms; `qlever_delta_triples` 0 → 14,000 | p50 3.4 / 3.2 / 3.3 ms; small p50 2.80 ms, p95 3.76 ms; same delta | **GO** |
| 4a | `ql:has-word` finds a literal inserted by Update, without a rebuild | **no** | yes | **NO-GO** on A (GO on B) |
| 4b | `ql:contains-word "zebra*"` (prefix) finds it, without a rebuild | **no** | **no** | **NO-GO** → `entity_labels` overlay |
| 5 | Updates survive a restart; restart time | yes; 2.1 s; also after `kill -9` (50 of 50 acknowledged updates kept); 9.2 s with 1M delta triples | yes; 2.0 s; `kill -9` 50 of 50; 12.2 s with 1M delta | **GO** |
| 6 | `SERVICE` with a 5,000-IRI `VALUES` sibling: pushdown, `service-max-value-rows` | pushed down (5,000 rows, 184 KB POST); above the limit nothing is pushed and the join silently returns 0 rows | same | **GO**, with the conditions in section 6 |
| 7 | Rebuild from scratch in a second directory while serving; 1B estimate | 10M: 13.0 s, serving unaffected (0 errors); 100M: 146 s; 300M: 508 s; **1B: about 30 min** | 10M: 6.9 s; 100M: 65 s; **1B: about 11 min** | **GO** |

## 1. Data (step 1)

`gen.py` writes 100,000 subjects × 100 triples = 10,000,000 triples (1.26 GB of N-Triples,
9,999,922 distinct): per subject one `rdf:type`, one unique `rdfs:label` of three pseudo-words and
the subject number, two unique 8–12-word notes, ten pooled material strings, ten links to a pool of
5,000 concepts, 40 links to other subjects, 20 decimals and 16 dates. The index has 422,002 text
records (literals) and 4,400,351 `ql:has-word` triples. No generated word starts with `z`, so the
only base literal a search for `zebra` can match is the control label of subject 0,
`"zebra-crossing-control"`. The same generator made the 1M, 30M, 100M, and 300M inputs of section 7.

## 2. Index build (step 2)

`qlever --qleverfile deploy/Qleverfile index` (what `make up` runs) in an empty directory, timed by
`peak_rss.sh` (wall clock; peak RSS sampled every 0.5 s with `ps`, and `/usr/bin/time -l`).

| Binary | Wall clock | Peak RSS (sampled / `time -l`) | Index on disk |
| :-- | --: | --: | --: |
| A | 13.1 s | 1,255 / 1,345 MiB | 0.27 GiB |
| B | 7.1 s | 1,124 / 1,414 MiB | 0.27 GiB |

## 3. SPARQL Update (step 3)

Client-side latency (Python `httpx`, localhost, including HTTP), with the server's `--persist-updates`.
Each single operation was repeated on 20 different subjects; the check column is the triple count
of the subject after each operation, the same every time.

| Operation | A p50 / p95 / max | B p50 / p95 / max | Check |
| :-- | :-- | :-- | :-- |
| `INSERT DATA` 100 triples about a new subject (one is `rdfs:label "zebra-crossing-xyz"`) | 3.42 / 3.96 / 4.00 ms | 3.37 / 4.14 / 8.95 ms | 100 |
| `DELETE DATA` of the same 100 triples | 3.23 / 3.91 / 3.92 ms | 3.18 / 3.97 / 6.02 ms | 0 |
| `DELETE WHERE { <s> ?p ?o }`, a base subject with 100 triples | 3.11 / 3.75 / 3.86 ms | 3.32 / 4.15 / 4.64 ms | 100 → 0 |
| 1,000 sequential small updates: one request `DELETE DATA {5 triples} ; INSERT DATA {5 triples}` on a different base subject each (a record edit as the sync worker sends it) | p50 2.85, p95 3.90, p99 4.56, max 19.4 ms | p50 2.80, p95 3.76, p99 4.01, max 9.84 ms | 1,000 edited labels |

The server's own time per update is under 1 ms (it reports whole milliseconds, mostly `0`).

**`qlever_delta_triples`** (Prometheus `/metrics`, needs the access token and `--enable-metrics`):
0 before, **4,000** after the 60 single operations, **14,000** after the 1,000 small updates, the
same as the `delta-triples` block of each update response. B also exports
`qlever_delta_triples{type="inserted"}` (5,000) and `{type="deleted"}` (9,000), and
`qlever_index_num_triples`.

Findings that matter for the sync worker (Step 1.11) and the rebuild trigger (Step 2.18):

- **The metric counts churn, not the net difference.** Deleting the 100 triples that were just
  inserted does not bring the count back to 0: they become 100 "deleted" delta triples. A record
  edited many times keeps growing the delta even if it ends where it started.
- **Several operations in one request work** (`DELETE DATA … ; INSERT DATA …`), and the response
  has one `delta-triples` block per operation.
- Every update response carries the warning `"SPARQL 1.1 Update for QLever is experimental."`
- **Bulk rate** (the replay after a rebuild): 1,000,000 triples in 100 `INSERT DATA` requests of
  10,000 took 49.9 s on A (20,000 triples/s; 495 ms per request at p50) and 75.1 s on B
  (13,300 triples/s; B also writes `ql:has-word` triples for every inserted literal).
- **A large delta slows queries.** With 1.01M delta triples on the 10M index, the probe queries of
  section 7 went from p50 8.0 to 22.5 ms (p95 24 to 38 ms) on A, and from 6.1 to 22.8 ms on B.

## 4. Text freshness (step 4)

After inserting `<…/s1/text-fresh> rdfs:label "zebra-crossing-xyz"` (and nothing else), each query
below was run against the live server without a rebuild. "Control" is the base-index literal
`"zebra-crossing-control"`, which every query should find; it tells a query that cannot work from
a literal that is not indexed. With `TEXT_INDEX = from_literals` every literal is a text record that
contains itself as an entity, so the working query form is
`?s rdfs:label ?t . ?text ql:contains-entity ?t . ?text ql:contains-word "zebra*"`. The shorter
`?t ql:contains-word "zebra*"` (the guide's shorthand) returns no rows, even for the control.

| Query on `?s rdfs:label ?t` | A: control / inserted | B: control / inserted | Latency |
| :-- | :-- | :-- | --: |
| `?text ql:contains-entity ?t ; ql:contains-word "zebra*"` (prefix) | yes / **no** | yes / **no** | 3–4 ms |
| `… ql:contains-word "zebra cross*"` | yes / no | yes / no | 2 ms |
| `… ql:contains-word "xyz"` (whole word of the new literal) | — / no | — / no | 1 ms |
| `?t ql:has-word "zebra"` | yes / **no** | yes / **yes** | 1.2–1.5 ms |
| `?t ql:has-word "xyz"` | — / no | — / yes | 1.1–1.5 ms |
| `?t ql:has-word "zebra-crossing-xyz"` (the guide's form) | **no** / no | **no** / no | 0.8 ms |
| `?t ql:has-word "zebra*"` | no / no | no / no | 0.8 ms |
| `?t ql:has-word ?w FILTER(STRSTARTS(STR(?w), "zebra"))` | yes / no | yes / yes | 84–87 ms |
| `FILTER(STRSTARTS(LCASE(STR(?t)), "zebra"))` | yes / yes | yes / yes | 92–94 ms |
| `FILTER(REGEX(?t, "(^\|[^a-z])zebra", "i"))` | yes / yes | yes / yes | 82 ms |

The results were the same after the restart of section 5 (on B, the inserted literal's
`ql:has-word` triples are persisted with the update).

- **Prefix search (`ql:contains-word "…*"`) never sees updated literals, on either binary.** The
  text index is built once by `qlever-index`; updates do not touch it.
- **Whole-word `ql:has-word` sees them only on B.** Upstream added `ql:has-word` triples for updated
  literals in `d417fa76` ("Add `ql:has-word` triples for literals inserted by an update", #3794),
  which is after the 0.6.0 release. A does not have it.
- **`ql:has-word` objects are single lower-case tokens.** QLever splits literals at non-alphanumeric
  characters, so `ql:has-word "zebra-crossing-xyz"` matches nothing, not even the base literal.
  Query each word separately.
- Prefix over the `ql:has-word` words with `FILTER(STRSTARTS(?w, …))` works on B, but it scans all
  has-word triples (84 ms here for 4.4M; LUX has 679M), so it is not an autocomplete path.
- `FILTER` over the labels themselves always sees updates but scans every label (90 ms for 100k).

**Verdict: NO-GO** for prefix freshness on both binaries, and for whole-word freshness on the
pinned binary. **Fallback (applied):** Phase 1 Step 1.13 unions the QLever prefix results with
`SELECT uri, default_label, type FROM entity_labels WHERE updated_at > <watermark time> AND
default_label ILIKE $1 || '%'`, so entities published since the last rebuild appear until the next
one. `entity_labels.updated_at` already exists for this (migration 0001). With a binary at or after
`d417fa76`, `ql:has-word` can serve whole-word matches for fresh records; the overlay is still
needed for prefixes.

## 5. Persistence and restart (step 5)

`restart.sh`: `make down` (stops only the server whose command line has `-p 7006`), wait for the
process to exit, `make up`, then poll until a query for the inserted triples succeeds.

| Case | A | B |
| :-- | :-- | :-- |
| Graceful restart, 14,002 delta triples | 2.08 s (stop 0.37, start until ready 1.68, first query 0.03) | 2.01 s |
| `kill -9` immediately after the 50th acknowledged single-triple `INSERT DATA`, then restart | 50 of 50 present; restart 2.52 s | 50 of 50; 2.08 s |
| Graceful restart with 1,014,052 delta triples (`lae.update-triples` 79 MB) | 9.19 s (7.3 s of it replaying the persisted updates) | 12.24 s |

After the graceful restart every update was present: 9,997,924 triples (9,999,922 − 20 × 100 deleted
+ 2 inserted), the 1,000 edited labels, no triples for the deleted subjects, the inserted literal,
and `qlever_delta_triples` = 14,002 as before. An update is on disk before QLever acknowledges it
(nothing acknowledged was lost to `kill -9`), so the sync worker can advance
`qlever_index_watermark` as soon as QLever answers 200.

**Verdict: GO.** Restart time grows with the delta (about 7 s per million delta triples on A), which
is one more reason to rebuild on a delta threshold.

## 6. SERVICE (step 6)

`stub_service.py` is a FastAPI endpoint on `127.0.0.1:9806` that parses each request with the
vendored LUX parser (`sparql_parser.py`, from `lux/qlever-middletier/sparql_service/sparql.py`),
logs what QLever sent, and answers `application/sparql-results+json`: one row per pushed-down IRI, or
a fixed "global top 20" when nothing was pushed (what a vector-first search returns). The query:

```sparql
SELECT ?x ?score WHERE {
  VALUES ?x { <…/s1/0> … <…/s1/4999> }
  SERVICE <http://localhost:9806/sparql> { ?x <urn:vec:score> ?score }
}
```

| Case | Pushed down? | What the stub received | Result rows | End to end (A / B) |
| :-- | :-- | :-- | --: | --: |
| 5,000 `VALUES`, default `service-max-value-rows` (10,000) | **yes** | POST `application/sparql-query`, 184,160 bytes, `VALUES (?x) { (<iri>) … }` with 5,000 rows (37 bytes per row) | 5,000 | 253 / 179 ms |
| 5,000 `VALUES`, limit set to 4,999 | **no** | 251 bytes, no `VALUES` | **0, no warning** | 27 / 21 ms |
| 20,000 `VALUES`, default limit | **no** | 251 bytes, no `VALUES` | **0, no warning** | 285 / 209 ms |
| 20,000 `VALUES`, limit 100,000 | yes | 749,160 bytes, 20,000 rows | 20,000 | 725 / 510 ms |
| Sibling computed from the index (`?x ex:dim0 ?d FILTER(?d < 250)`, 5,216 rows) | yes | 197,935 bytes, 5,216 rows | 5,216 | 209 / 130 ms |

The runtime information of every query reports `optimized-with-sibling-result: yes|no`, which is how
the pushdown can be checked from the client. The stub spent 10–16 ms on 5,000 rows and 31–37 ms on
20,000. A sends `Accept: application/sparql-results+json`; B sends
`application/vnd.qlever.results.v1,application/sparql-results+json;q=0.9`, so the Phase 3 service must
keep answering JSON (or implement QLever's binary format). `service-max-value-rows` can be set at
start (`-S`) or at runtime (`?service-max-value-rows=N` with the access token).

**Verdict: GO**, with the conditions already listed in `docs/reuse_map.md`: above
`service-max-value-rows` QLever pushes nothing and the join silently returns almost nothing, so the
Phase 3 vector `SERVICE` must raise the limit for its expected candidate sizes, use
`vec:mode "constrained"` (refuse to run without `VALUES`), and tests should assert
`optimized-with-sibling-result: yes`. Open Question 1 (`SERVICE` versus application-layer fusion)
can be decided on these numbers in Phase 3.

## 7. Rebuild while serving (step 7)

`rebuild.sh`: while the 7006 server (blue, with 1.01M delta triples) answered a round-robin of three
queries (a 100k-row join count, a `contains-word` prefix query, a subject lookup; query cache off)
every 50 ms, the same 10M input was indexed from scratch into a second directory with the same
Qleverfile, and that index was then started on 7306 (green) and checked (9,999,922 triples).

| Binary | Build (wall, peak RSS) | Green start | Blue during the build: queries, errors, p50 / p95 / max | Blue without a build (20 s): p50 / p95 / max |
| :-- | :-- | --: | :-- | :-- |
| A | 13.0 s, 1,284 MiB | 1.66 s | 198, 0, 9.7 / 16.8 / 28.3 ms | 12.8 / 26.7 / 29.4 ms |
| B | 6.9 s, 1,030 MiB | 1.68 s | 104, 0, 8.2 / 20.2 / 52.0 ms | 12.7 / 21.2 / 32.5 ms |

Serving was not affected beyond noise: no errors, and the medians stayed where they were.

**Scaling.** Up to 100M with `rebuild.sh` (the blue server kept serving throughout); 300M, and
B's 30M and 100M, with `scale.sh` (no blue server).

| Binary, triples | Build wall clock | Peak RSS | Index on disk | Blue during the build (p50 / p95 / max, errors) |
| :-- | --: | --: | --: | :-- |
| A, 1M | 1.8 s | 0.41 GiB | | 13.4 / 20.3 / 23.3 ms, 0 |
| A, 10M | 13.0 s | 1.30 GiB | 0.27 GiB | 13.5 / 19.9 / 27.0 ms, 0 |
| A, 30M | 39.3 s | 2.30 GiB | | 13.6 / 22.5 / 37.7 ms, 0 |
| A, 100M | 145.7 s (99.6 s triples + 45.8 s text index) | 3.88 GiB | 2.8 GiB | 14.4 / 26.5 / 49.3 ms, 0 of 2,086 |
| A, 300M | 507.8 s (351.7 s triples + 155.4 s text index) | 4.13 GiB | 8.2 GiB | — |
| B, 10M | 6.9 s | 1.01 GiB | 0.27 GiB | 8.2 / 20.2 / 52.0 ms, 0 |
| B, 30M | 20.6 s | 2.21 GiB | 0.84 GiB | — |
| B, 100M | 65.1 s (21.9 s triples + 42.8 s text index) | 4.75 GiB | 2.8 GiB | — |

Peak RSS levels off at about 4–5 GiB because `STXXL_MEMORY = 4G` caps the sort buffers; above that
the build spills to disk, which is why the time grows slightly faster than linearly. Other agents
pushed the load average to 13–17 during the first half of the 300M build (parsing ran at 2.3M
triples/s against 5.3M at 10M), so that point is, if anything, pessimistic.

**Extrapolation to 1B triples.** Method (`extrapolate.py`): a least-squares fit of
`log(time) = log(a) + b · log(triples)` over the builds from 10M up (the 1M build is dominated by
fixed start-up cost), evaluated at 1e9, next to linear scaling of the largest build and the local
slope between the two largest builds.

| Binary | Points | Fitted exponent b | 1B, power-law fit | 1B, linear from largest | 1B, slope of the last two points |
| :-- | :-- | --: | --: | --: | --: |
| A | 10M, 30M, 100M, 300M | 1.08 | **1,800 s (30 min)** | 1,693 s (28 min) | 1,995 s (33 min, b = 1.14) |
| B | 10M, 30M, 100M | 0.97 | **618 s (10 min)** | 651 s (11 min) | 588 s (10 min, b = 0.96) |

So a 1B-triple rebuild with the laptop settings takes **about half an hour on A** (28–33 min) and
about 10–11 min on B, with an index of roughly 28 GiB and 130 GB of uncompressed N-Triples input.
A real-data calibration point agrees: an existing LUX index on this machine (QLever 0.5.50, 332.6M
input triples plus 679M `ql:has-word` triples, `STXXL_MEMORY = 90G`, no text index) took 539 s for
the triple index, which scales linearly to about 27 min per billion input triples. Linked Art is
more text-heavy than the synthetic data (section 9), so budget the text index separately: it was
31 % of the 300M build. Not included: the Postgres snapshot and N-Triples export (Spike S6) and
the replay of outbox events past the watermark (seconds, at the rates in section 3).

**QLever's own in-process rebuild** (`?cmd=rebuild-index`, present in both binaries) was measured
for comparison: 2.0 s to fold the 1.01M delta triples into a new 11.0M-triple index while serving
(31 queries, 0 errors), and the delta went back to 0. **But it drops the text index**: QLever logs
`A text index was loaded for the current index, but text search will no longer work after the
rebuild completes`, and afterwards `ql:contains-word` finds nothing, not even the control. On A
the rebuilt index also lacks `ql:has-word` triples for updated literals. So while autocomplete uses
the text index, Phase 2 Step 2.18 must keep the external blue/green build with `qlever-index`
(ADR-0002) and must not use `cmd=rebuild-index` or `--rebuild-index-strategy automatic`.

**Verdict: GO.** A full rebuild runs beside the serving index without disturbing it; the swap
itself costs a green start of about 1.7 s plus the replay of the outbox events past the watermark
(about 350 small edits/s or 20,000 triples/s per section 3).

## 8. Source build of `lux/qlever-dev` (binary B)

Built out of tree, never writing into the source checkout (its `git status` stayed clean):

```sh
HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_UPGRADE=1 HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1 \
  brew install cmake ninja pkgconf      # additive: cmake 4.5.0, ninja 1.13.2, pkgconf 3.0.10
cmake -S /Users/rs2668/Development/lux/qlever-dev -B /tmp/lae-i6/build -GNinja \
  -DCMAKE_BUILD_TYPE=Release -DLOGLEVEL=INFO -D_NO_TIMING_TESTS=ON \
  -DICU_ROOT=/opt/homebrew/opt/icu4c@78 -DOPENSSL_ROOT_DIR=/opt/homebrew/opt/openssl@3 \
  -DCMAKE_CXX_FLAGS=-DMETA_NO_STD_FORWARD_DECLARATIONS
ninja -C /tmp/lae-i6/build -j 12 qlever-index qlever-server
```

- Existing Homebrew libraries were enough: Boost 1.90, ICU 78 (`icu4c@78`, the one the `qlever`
  formula uses), OpenSSL 3, jemalloc 5.4, zstd 1.5.7. CMake fetched the other dependencies (abseil,
  antlr, range-v3, re2, s2, spatialjoin, opentelemetry-cpp, …) into the build directory.
- **One workaround:** with AppleClang 21 (Xcode command-line tools 21.0.0) range-v3's forward
  declarations of `std::allocator`, `std::pair`, … clash with libc++ ("redefinition of 'allocator'
  as different kind of symbol"). `-DMETA_NO_STD_FORWARD_DECLARATIONS` turns them off. Upstream's
  macOS CI uses macOS 15's older AppleClang, so it does not hit this.
- Configure 51 s; compile about 2 min 50 s with 12 jobs (9 s until the range-v3 error, then
  2 min 39 s). The two binaries are 71 and 77 MB, the build directory 2.5 GB. It reports version `0.0.0-unknown` and an empty git hash because QLever's
  `CompilationInfo.cmake` runs `git` in the build directory; the commit is `ba7b74c`.
- The build directory is kept at `/tmp/lae-i6/build` for later steps (`run_s1.sh <label>
  /tmp/lae-i6/build` puts its binaries first on `PATH`). `/tmp` is not durable; rebuild with the
  commands above if it is gone.

## 9. Measurement notes

- All latencies are client-side over localhost HTTP from Python, so they include about 0.5–1 ms of
  client overhead. The machine was shared with other agents' builds and servers. Two full runs of A
  differed by up to 40 % on the `SERVICE` cases (176 ms versus 253 ms for 5,000 `VALUES`) and by less
  than 15 % on the update latencies. Treat the numbers as orders of magnitude.
- Index build times use the Qleverfile's laptop settings (`STXXL_MEMORY = 4G`, `num-triples-per-batch`
  500,000). A production host with more memory builds faster at large sizes.
- The synthetic data is lighter on text than Linked Art: 0.44 `ql:has-word` triples per triple here,
  about 2 in the LUX index above.

## 10. How to reproduce

```sh
export LAE_QLEVER_ACCESS_TOKEN=<any secret>
scripts/spikes/s1_qlever/run_s1.sh homebrew                         # binary A, steps 1-7
scripts/spikes/s1_qlever/run_s1.sh master-ba7b74c /tmp/lae-i6/build # binary B
uv run --project backend python scripts/spikes/s1_qlever/gen.py --subjects 1000000 --out /tmp/lae-i6/data/s1-100m.nt
scripts/spikes/s1_qlever/scale.sh /tmp/lae-i6/data/s1-100m.nt /tmp/lae-i6/results/scale/homebrew-100m.txt
uv run --project backend python scripts/spikes/s1_qlever/extrapolate.py 1e7=<log> 3e7=<log> 1e8=<log> 3e8=<log>
```

| File | What it does |
| :-- | :-- |
| `gen.py` | Synthetic N-Triples (step 1). |
| `run_s1.sh` | Steps 2–7 for one binary, through `deploy/Qleverfile` and `make up` / `make down`; results in `/tmp/lae-i6/results/<label>/`. |
| `bench.py` | The measurements against a running server: `updates`, `text`, `persist`, `service`, `bulk`, `probe`, `marker` / `count-markers`, `inplace-rebuild`. |
| `peak_rss.sh` | Wall clock and peak RSS of a command. |
| `restart.sh`, `crash.sh` | Timed restart; `kill -9` after acknowledged updates. |
| `stub_service.py`, `sparql_parser.py` | The stub `SERVICE` and the vendored LUX request parser. |
| `rebuild.sh`, `scale.sh`, `extrapolate.py` | Step 7: build beside a serving index; scaling builds; the 1B fit. |
