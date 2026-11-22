# Spike S6: bulk bootstrap timing

**Result: GO.** One million synthetic Linked Art records go through the whole bootstrap
(`COPY` into `records`, pyld expansion to one N-Triples file, QLever index build) in **10.0 min**
with the pipeline as it is on `main`, and in **6.0 min** with two expansion tunings that change no
output byte. The projection to **10M records is 1.8 h (as on `main`) or 1.2 h (tuned)**, well
inside the 8-hour GO line, with a QLever peak of about 4–5 GiB. The bottleneck is **pyld
expansion**: 70 % of the 1M wall clock, 98 % of it inside `jsonld.to_rdf`, and most of that is
pyld re-processing the pinned context rather than expanding data. One loader flag
(`tag: "static"`) is worth +32 %; Step 1.3 should adopt it.

| | |
| :-- | :-- |
| Step | Phase 0, Step 0.11 (issue #11) |
| Date | 2026-11-22 |
| Machine | Apple M5 Max, 18 cores (6 performance + 12 efficiency), 128 GB, internal SSD; macOS 26.6 |
| Software | Python 3.12.11 (uv), pyld 3.3.0, asyncpg 0.32.0, orjson 3.13.0; PostgreSQL 18.7 (Postgres.app, stock settings, §3.2); QLever 0.6.0 (Homebrew `qlever-index` / `qlever-server`, git `6c5861a`) driven by qlever-control 0.6.0 and `deploy/Qleverfile` |
| Data | 1,000,000 synthetic records, seed `20261122`, 4.36 GB of JSON Lines (§2.1) |
| Database | Slot database `lae_i11` at migration 0001 (`uv run lae migrate`); dropped afterwards |
| QLever | Index name `lae_i11` in fresh directories under `/tmp/lae-i11/`; server on **port 7311**, because this slot's port 7011 was already taken by an unrelated `qlever-server -i nrich` |
| Scripts | `scripts/spikes/s6_bootstrap/`: `generate.py`, `load.py`, `export_nt.py`, `build_index.py`, `ntriples.py` (expansion worker), `sanitize.py` (vendored), `test_s6.py` |
| Load | Shared machine: a CrashPlan backup used 1–1.8 cores throughout, and another agent's `qlever-index` used up to 12 cores for a few minutes. The first expansion scaling runs overlapped it and were discarded and repeated. The runs reported below had the machine to themselves apart from the backup (1-minute load 4.5–10 before each run, except where the previous run of this spike was still in the average; during the 18-worker runs the load is the workers themselves). |

## 1. Question and GO line

How long does each bootstrap stage take for 1M records (`COPY` rate, expansion rate, index
build), how big is the N-Triples, how much memory does the index build need, which stage is the
bottleneck, and what does that project to at 10M? Fallback if the answer is "too slow": tune the
expansion pipeline before the Phase 1 load (`docs/implementation_phases.md`).

The guide sets no threshold, so this spike uses: **GO** if the projected 10M bootstrap, run
stage after stage on one machine like this one, finishes inside an **8-hour overnight window** and
no stage needs more memory than the machine has.

## 2. Pipeline and method

```
generate.py ──JSONL──▶ load.py ──COPY──▶ records ──cursor──▶ export_nt.py ──.nt──▶ build_index.py
 (synthetic input)     (asyncpg binary     (jsonb, lz4)       (process pool:        (qlever index,
                        COPY, 10k batches)                     pyld + sanitation)    Qleverfile)
```

Every stage prints a JSON summary; each number below comes from one of those summaries.

### 2.1 Synthetic records (`generate.py`)

Record `i` depends only on `(seed, i)`, so the output is identical whatever the parallelism:
generating the 1M file with 18 workers and 10k-record chunks, and again with 7 workers and 25k
chunks, gave the same SHA-256 (`8f6d1ae8…6012695e`). Each record:

1. **Class** from one seeded stream over a museum-plus-library mix (table below).
2. **Template:** a corpus record of that class (`lae.corpus.iter_corpus()`; institutional records
   weighted 4× a linked.art example), with 0–4 donor templates of the same class merged in (list
   properties concatenated, missing properties copied), so records combine the patterns of several
   examples and reach realistic sizes.
3. **Identifiers:** a UUIDv7 record URI (`https://collection.example.org/id/<uuidv7>`) and, for
   every embedded node without an `id`, an internal node IRI `<record>/node/<uuidv7>` (S3 rule R4;
   `lae.ids.record_id_from_uri` accepts all of them, checked in `test_s6.py`). There are no blank
   nodes left (the index has 0).
4. **References:** local references point at other synthetic records of the same class;
   authority references (AAT, ULAN, Wikidata, LoC, …) are kept; web resources (`access_point`,
   `conforms_to`, …) get new paths.
5. **Literals:** names from corpus words (Zipf-sampled) or synthetic proper names for people and
   groups, identifiers with new digits, statements resampled with a log-normal length factor,
   dates shifted by one offset per record, dimension values scaled.
6. **Defects:** 1 record in 10,000 gets one sanitation defect (§3.4), so the export's sanitation
   counters can be checked against known numbers.

| Class | Share | Records | Mean JSON bytes | Mean triples |
| :-- | --: | --: | --: | --: |
| HumanMadeObject | 35 % | 350,075 | 5,443 | 96.2 |
| Person | 15 % | 149,872 | 2,456 | 48.3 |
| LinguisticObject | 15 % | 149,452 | 6,399 | 115.6 |
| Type | 8 % | 80,124 | 1,112 | 20.5 |
| VisualItem | 8 % | 79,981 | 5,885 | 97.0 |
| Group | 5 % | 50,190 | 3,389 | 59.6 |
| Place | 4 % | 40,380 | 1,697 | 33.4 |
| Set | 4 % | 40,003 | 2,082 | 36.8 |
| DigitalObject | 3 % | 29,918 | 3,670 | 59.5 |
| Activity | 2 % | 19,966 | 5,900 | 70.5 |
| Event | 0.5 % | 5,018 | 709 | 12.7 |
| Period | 0.25 % | 2,500 | 651 | 11.7 |
| PropositionalObject | 0.25 % | 2,521 | 2,149 | 39.2 |
| **All** | | **1,000,000** | **4,359** | **75.5** |

Bytes per class are over the first 20,000 records, triples over the first 5,000; the "All" row is
the whole 1M. Compact record size: p50 2,319 B, p90 12,345 B, p99 23,700 B, max 59,508 B. For
comparison, the institutional corpus records average 10.4 KB (HumanMadeObject), 1.4 KB (Person)
and 0.45 KB (Type) compact. Generating the 1M file took 6.8 s (146k records/s, 18 workers); it is
the bootstrap's input, not one of its stages.

### 2.2 `COPY` (`load.py`)

Reads the JSON Lines, builds one row per record (`id` from the URI's UUID, `uri`, `type`,
`version = 1`, `status = 'active'`, `etag` = BLAKE2b-128 of the canonical JSON, `data` = the line as
`jsonb`, `context_version = 1`) and writes batches of 10,000 with asyncpg's binary
`copy_records_to_table`, into the migration-0001 table with all its indexes in place
(primary key, unique `uri`, `type`). `--streams N` runs N processes with one connection each,
taking every N-th batch. Each run started from `TRUNCATE records CASCADE` and a `CHECKPOINT`.

### 2.3 Expansion (`export_nt.py`, `ntriples.py`)

The main process streams `SELECT data::text FROM records WHERE status = 'active'` through a
server-side cursor in batches of 500 and hands each batch to a `ProcessPoolExecutor` worker; at
most 3 × workers batches are outstanding. A worker parses the record (orjson), sanitizes ids,
expands it with `pyld.jsonld.to_rdf` and the pinned context through
**`lae.rdf.context.document_loader`** (from Step 0.9, on `main`; never touches the network), and
serializes sanitized N-Triples. The main process appends the bytes to one `.nt` file.

Two tuning experiments, off by default, both producing byte-identical triples (§3.3):

- `--static-context`: the same loader, but the returned remote document carries
  `"tag": "static"`. pyld keeps a resolved remote context in its shared cross-document cache only
  when it is tagged `static`; untagged, every `to_rdf` call reloads, re-resolves and
  re-canonicalizes the whole 79 KB context (with `lae.rdf.context`, it also re-parses the file).
- `--memo-c14n`: memoizes pyld's JCS canonicalization of context objects. pyld canonicalizes
  each type-scoped context every time it applies one (about 70 times per record here) to look it
  up in its cache; those objects are the same Python objects every time, so the memo keys on the
  identity of their values. This patches a pyld internal (`pyld.context_resolver.canonicalize`).

### 2.4 Index (`build_index.py`)

`qlever --qleverfile deploy/Qleverfile index --name lae_i11` in a fresh directory, with the
Qleverfile's settings unchanged (on-disk-compressed vocabulary, parallel parsing,
`STXXL_MEMORY = 4G`, `num-triples-per-batch = 500000`, text index from literals,
`ql:has-word` triples). Peak RSS is the kernel's high-water mark for the largest descendant
(`getrusage(RUSAGE_CHILDREN).ru_maxrss`), cross-checked with a 0.5 s `ps` sampler over the process
tree and with `qlever-index`'s own 1 s resource log. At 1M the three agree within 0.1 GiB (4.02,
3.99, 3.93); on the smaller builds the samplers miss short peaks, so the tables use the kernel
figure. Phase times
come from the index log. `serve-check` then starts the index on port 7311 (`PERSIST_UPDATES` from
the Qleverfile), counts all triples, runs one text query, and stops exactly that server.

To extrapolate the index build, the same build was run on the first 10 % and 30 % of the
N-Triples lines (`head -n`).

## 3. Results at 1M records

### 3.1 Summary

| Stage | As on `main` | Tuned | Rate | Share of the 1M pipeline (main / tuned) |
| :-- | --: | --: | :-- | --: |
| `COPY` | 38.8 s (1 stream) | 18.0 s (4 streams) | **25,767 / 55,423 records/s** | 6 % / 5 % |
| Expansion | 418.8 s | 199.6 s | **2,388 / 5,009 records/s** (180k / 378k triples/s), 18 workers | **70 %** / 55 % |
| Index build | 145.3 s | 145.3 s | 519k input triples/s; **peak RSS 4.02 GiB** | 24 % / 40 % |
| **Total** | **602.9 s (10.0 min)** | **362.9 s (6.0 min)** | | |

| Output | Value |
| :-- | :-- |
| N-Triples | **14,492,068,456 bytes (14.5 GB)**, **75,450,650 triples**; 75.5 triples and 14.5 KB per record; 192 bytes per triple (node IRIs are ~110 characters) |
| Triples in QLever | 51,941,524 distinct (68.8 % of the input; see finding 5); 83,710,379 `ql:has-word` triples |
| Index on disk | 2.41 GB |
| `records` in Postgres | 2.65 GB (heap 1.11 GB, TOAST/lz4 1.40 GB, indexes 0.14–0.22 GB); WAL written per load 2.7–3.2 GB |

### 3.2 `COPY`

| Streams | Wall | Records/s | Input MB/s | Client prep | Time inside `COPY` (sum) | Postgres backend CPU (sum) |
| --: | --: | --: | --: | --: | --: | --: |
| 1 | 38.81 s | **25,767** | 112 | 9.1 s | 27.6 s | 24.0 s |
| 4 | 18.04 s | **55,423** | 242 | 9.9 s | 49.5 s | 32.4 s |
| 8 | 17.15 s | **58,317** | 254 | 11.6 s | 85.7 s | 42.5 s |

With one stream the rate did not fall as the table grew: per 100k rows it went 22.5k, 24.4k,
24.5k, 24.9k, 27.7k, 27.0k, 27.8k, 26.8k, 26.6k, 27.0k records/s. The single backend is 87 % busy
inside `COPY` (JSONB input and lz4 TOAST compression) and client preparation runs in series with
it. Four streams double the rate; eight add almost nothing: the backends are busy only 45 % and
31 % of the time, waiting on WAL and buffers with Postgres.app's stock settings
(`shared_buffers = 128MB`, `max_wal_size = 1GB`, `wal_compression = off`,
`synchronous_commit = on`, `full_page_writes = on`). These settings were not changed because the
server is shared; a production server sized for 10M records would raise `shared_buffers` and
`max_wal_size` and turn on `wal_compression`, which should move the plateau (not measured).

### 3.3 Expansion

Same 100,000 records (`ORDER BY id LIMIT 100000`) each time:

| Workers | Loader | Wall | Records/s | Worker CPU per record | Worker busy |
| --: | :-- | --: | --: | --: | --: |
| 1 | as on `main` | 20.7 s (5,000 records) | 242 | 4.1 ms | 100 % |
| 6 | as on `main` | 84.7 s | 1,180 | 5.0 ms | 98 % |
| 12 | as on `main` | 55.1 s | 1,816 | 6.4 ms | 97 % |
| 18 | as on `main` | 40.3 s | **2,482** | 6.7 ms | 92 % |
| 18 | + `static` tag | 30.5 s | **3,283** (+32 %) | 4.9 ms | 90 % |
| 18 | + `static` tag + c14n memo | 21.4 s | **4,667** (+88 %) | 3.5 ms | 91 % |

At 1M records the two configurations ran at 2,388 and 5,009 records/s (2.10×), and the two
outputs are identical: 75,450,650 lines each with the same order-independent checksum (sum of
64-bit BLAKE2b per line, `30219f0c88fdaf7d`); `test_s6.py` checks byte equality on 300 records.

Where the time goes:

- **pyld `to_rdf` is 98.2 %** of worker time (orjson parse 0.2 %, sanitation and serialization
  1.5 %). Workers are 95 % busy; the main process used 12.6 CPU-s of 419 s (3 %) and the Postgres
  backend 11.9 s, so feeding the pool is not a limit.
- cProfile of a single process (300 records): untagged, re-resolving the remote context per
  record is ~40 % of the time and JCS canonicalization of contexts ~55 % (the two overlap). With
  the `static` tag, applying type-scoped contexts (`_process_context`) is 67 %, of which
  canonicalization is 29 points. With the memo as well, `_process_context` is still 55 %: pyld
  clones the active context whenever it applies a non-propagating type-scoped context and gives the
  clone a new `_uuid`, so its per-context cache of processed contexts never hits, and every typed
  node rebuilds its term definitions (~145 `_create_term_definition` calls per record). Caching
  processed type-scoped contexts is the next tuning step; the profile bounds its gain at about 2×
  (not measured).
- Scaling is sublinear on this machine because the efficiency cores are slower: worker CPU per
  record rises from 4.1 ms (one worker, on a performance core) to 6.7 ms with 18 workers. On a
  server with uniform cores, expect close to linear scaling with cores.
- Alternative considered: pyoxigraph 0.5.13 parses JSON-LD but has no document-loader hook.
  With the context inlined into each record it ran at 126 records/s on the corpus (pyld untagged:
  496/s on the same records) and 35 of 244 corpus records gave a different triple count. Not an
  alternative today.

### 3.4 Sanitation

`clean_iri`, `clean_datetime` and `pad_datetime` are vendored from `lux/data-pipeline`
(`sanitize.py`). They run twice: on every `id` before expansion, because **pyld silently drops
IRIs that contain whitespace** (and every triple using them) and passes `"` through, which breaks
the QLever build; and on every IRI and `xsd:dateTime` literal after expansion. Literals are escaped
for N-Triples (`\`, `"`, newline, carriage return, tab).

| Injected defect | Injected | Counted by the export | Effect |
| :-- | --: | --: | :-- |
| IRI with a space | 23 | `iri_repaired` 23 | percent-encoded, kept |
| IRI with no host (`https:/no-host/…`) | 12 | `iri_rejected` 12 | node removed (reject) |
| Year-only `xsd:dateTime` | 9 | `datetime_repaired` 9 | padded to start or end of the year |
| Unparseable `xsd:dateTime` (`c. 1850`) | 9 | `datetime_rejected` 9 | triple removed (reject) |
| Label with `"`, `\`, newline, tab | 34 | — | escaped |

No record failed expansion and none was rejected. QLever parsed exactly as many triples as the
file has lines at all three sizes (7,545,065 / 22,635,195 / 75,450,650), so nothing was silently
dropped, and `SELECT (COUNT(*) …)` on the server returned 51,941,524, the index's distinct count.

### 3.5 Index build

| Input (lines of the 1M file) | Triples | Wall | Peak RSS | CPU | Index | Parse | Vocabulary merge | ID conversion + permutations | Text index |
| :-- | --: | --: | --: | --: | --: | --: | --: | --: | --: |
| first 10 % | 7.5M | 16.9 s | 1.21 GiB | 43 s | 0.27 GB | 2.1 s | 0.6 s | 4.1 s | 9.5 s |
| first 30 % | 22.6M | 46.6 s | 2.21 GiB | 131 s | 0.76 GB | 5.9 s | 1.3 s | 13.4 s | 25.7 s |
| all | 75.5M | **145.3 s** | **4.02 GiB** | 458 s | 2.41 GB | 19.6 s | 4.0 s | 38.8 s | **82.6 s** |

The peak RSS occurs while triples are converted to global IDs (26 s into the 1M build). The
**text index is 57 % of the build and runs on one core** (`qlever-index` at ~100 % CPU for those
83 s, against ~1,100 % while parsing). This synthetic data is text-heavy (statements resampled from
corpus words); in Spike S1's data the text index was 31 % of the build.

Serving the 1M index: ready in 1.5 s, `COUNT(*)` in 0.35 s, `ql:contains-word "portrait*"`
60,338 hits in 0.01 s, server RSS 0.07 GiB after start-up; stopped afterwards.

## 4. Projection to 10M records

| Stage | Method | 10M as on `main` | 10M tuned |
| :-- | :-- | --: | --: |
| `COPY` | Linear in rows: the per-100k rate stayed flat or rose over the 1M load, and the B-tree indexes on time-ordered UUIDv7 and URIs grow at their right edge | 6.5 min (1 stream) | 3.0 min (4 streams) |
| Expansion | Linear in records: records are independent, pyld's caches are warm after 0.2 s, and the 100k and 1M runs agree within 7 % | 69.8 min (19.9 CPU-hours) | 33.3 min (9.5 CPU-hours) |
| Index build (754.5M input triples) | Power law `time ∝ triples^b` from the 1M build, with `b` from Spike S1's builds of the same binary and Qleverfile at 10M–300M triples: `b = 1.08` (fit), `1.14` (slope of the last two points). This spike's own three points give `b = 0.94`, but they are dominated by fixed costs | 29–33 min | 29–33 min |
| **Total, stage after stage** | | **≈ 1.8 h** | **≈ 1.2 h** |

- **Memory:** the index build's peak RSS levels off at about 4–5 GiB with `STXXL_MEMORY = 4G`:
  4.02 GiB here at 75M triples, and 3.88 / 4.13 GiB at 100M / 300M in S1. Nothing comes near
  128 GB.
- **Disk:** 145 GB of N-Triples, about 24 GB of index, about 27 GB for `records` plus 27–32 GB of
  WAL written during the load. Writing the export compressed (Step 1.12 allows gzip; a parallel
  compressor would be needed to keep up with 73 MB/s) or streaming it straight into `qlever-index`
  would avoid the 145 GB file (not measured).
- **Phase 1 shape (estimate, not measured at scale):** Step 1.3's `strip_stub_attributes` replaces
  every foreign reference with `{"id": uri}` before expansion. On 3,000 generated records that cuts
  triples per record from 76.2 to 49.4 (−35 %), N-Triples bytes by 24 %, and expansion time by
  37 % (untagged) to 52 % (tuned) in one process. The 1M numbers above are therefore an upper bound
  for the Phase 1 bootstrap.
- **Text index:** single-threaded; about 14 min of the 10M index build if it stays linear.
  S1 found a newer QLever (master `ba7b74c`) 2.2× faster on a 100M build (4.5× on the triple
  part, the text index about the same).

## 5. Findings for Phase 1

1. **pyld expansion is the bottleneck, and most of it is context overhead.** Step 1.3 should
   return `"tag": "static"` from `lae.rdf.context.document_loader` (and keep the parsed context
   instead of re-reading the file per call): +32 % here, and the same saving applies to every
   per-record expansion (Step 1.9 validation, where S4 found pyld to be most of the cost; the sync
   worker). The c14n memo (+42 % more) patches a pyld internal; decide whether to carry it or
   upstream it (`docs/questions.md`, Step 0.11). Caching processed type-scoped contexts is the next
   step after that.
2. **Sanitize ids before expansion, not only after.** pyld drops whitespace-containing IRIs
   without an error, so a post-expansion check alone never sees them; `ntriples.sanitize_ids`
   shows the pre-pass (Step 1.3, Step 1.12).
3. **Blank nodes:** pyld numbers blank nodes from `_:b0` in every document, so concatenated
   exports would merge unrelated nodes. With internal node IRIs on every typed node there were
   none here; Step 1.3's `BlankNodeError` is the right guard. The spike relabels them per record.
4. **qlever-control rejects absolute input paths.** `QLEVER_INPUT_FILES=/abs/path.nt` fails with
   "Non-relative patterns are unsupported" (it globs relative to the index directory).
   `lae bootstrap build-index` must link or copy the `.nt` into the index directory, as
   `build_index.py` does.
5. **Foreign-subject triples.** Exported as stored, 27 % of the triples have a subject the record
   does not own (AAT terms' `rdf:type`, `rdfs:label` and nested `classified_as`; other records'
   type and label): 2.05M of the first 7.5M lines, only 74k of them distinct. They are 95 % of the
   31 % duplicate triples QLever removed. Step 1.3's `strip_stub_attributes` already keeps them out
   of the Phase 1 export (spec §3.3), which also matters for correctness: a revision diff that
   deleted such a triple would delete it for every record that still asserts it.
6. **`COPY` is cheap; use 4 streams.** Four parallel streams give 2.2× one stream on stock
   settings. The table's indexes can stay in place during the load.
7. **Budget the text index separately.** It is single-threaded and was the largest index phase.

## 6. GO / NO-GO

**GO.** The projected 10M bootstrap takes about 1.8 h as on `main` and 1.2 h with the two
tunings, against an 8-hour window, and the largest memory peak is about 4–5 GiB. Expansion is the
bottleneck at every scale (70 % of the 1M pipeline), so the fallback's advice still applies as a
recommendation: adopt the `static` loader tag in Step 1.3 before the Phase 1 load (finding 1).

## Appendix A. Reproducing

From `backend/`, with `LAE_DATABASE_URL=postgresql+asyncpg://localhost:5432/lae_i11` (create the
database, apply `deploy/postgres/init.sql`, run `uv run lae migrate`):

```
uv run pytest -c pyproject.toml ../scripts/spikes/s6_bootstrap -q          # 10 passed
uv run python ../scripts/spikes/s6_bootstrap/generate.py --count 1000000 --out /tmp/lae-i11/records.jsonl
uv run python ../scripts/spikes/s6_bootstrap/load.py --input /tmp/lae-i11/records.jsonl --truncate [--streams 4]
uv run python ../scripts/spikes/s6_bootstrap/export_nt.py --out /tmp/lae-i11/records.nt --workers 18 \
    [--static-context [--memo-c14n]] [--limit 100000]
uv run python ../scripts/spikes/s6_bootstrap/build_index.py index --nt /tmp/lae-i11/records.nt \
    --dir /tmp/lae-i11/qlever-records
lsof -i :7311    # must be free
uv run python ../scripts/spikes/s6_bootstrap/build_index.py serve-check \
    --dir /tmp/lae-i11/qlever-records --port 7311
```

Every command accepts `--stats-out <file.json>`. `serve-check` stops only its own server (its
`--cmdline-regex` matches the unique index name and the port); never run a bare `qlever stop`.
