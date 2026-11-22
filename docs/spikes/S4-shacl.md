# Spike S4: SHACL latency

**Result: GO.** Validating one `HumanMadeObject` record (pyld expansion, data graph, pyshacl)
takes **3.9 ms at p50 and 8.8 ms at p95** over the 99 corpus objects with the recommended
configuration: pyshacl with the shapes loaded once and a **pyoxigraph** data store. pyshacl alone
takes 1.7 ms (p50) and 3.5 ms (p95). Even the naive configuration stays under the 100 ms line
(p95 42.1 ms). Recommended budget: **50 ms per record**. Two of the ten shapes (`xsd:dateTime`
validity and ordering) should move to the Python rule engine, for correctness rather than latency
(finding 6).

| | |
| :-- | :-- |
| Step | Phase 0, Step 0.9 (issue #9) |
| Date | 2026-11-22 |
| Machine | Apple M5 Max (Apple Silicon, arm64), 18 cores, 128 GB; macOS 26.6.2 |
| Software | Python 3.12.11 (uv), pyshacl 0.40.2, rdflib 7.6.0, pyld 3.3.0, pyoxigraph 0.5.13 |
| Corpus | The 99 `HumanMadeObject` records in `tests/corpus/` (94 linked.art, 5 institutional): 2 to 316 triples, mean 29 |
| Shapes | `profiles/shapes/HumanMadeObject.ttl`: 10 shapes, codes `LAE-SHACL-001` to `-010` |
| Script | `scripts/spikes/s4_shacl/bench.py` (raw samples: `--out`, default `/tmp/lae-i9/s4-results.json`) |
| Load | Other Phase 0 agents shared the machine (QLever index builds); 1-minute load average 4.41 at the start and 4.10 at the end. The benchmark is single-threaded. |

## 1. Question

How long does pyshacl take to validate one record, and which configuration keeps the per-record
p95 under 100 ms (the GO line)? The guide asks for `advanced=False`, a pre-loaded shapes graph,
and pyoxigraph as the data store. If the p95 is over the line, which constraints move to the
Python rule engine (fallback in `docs/implementation_phases.md`)?

## 2. Setup

### 2.1 Pipeline

Each timed iteration runs the whole per-record path Step 1.9 will run, in three timed stages:

1. **expand**: `pyld.jsonld.to_rdf` on the record with the pinned context. The document loader is
   `lae.rdf.context.document_loader` (new in this step, for Phase 1 Step 1.3 to reuse): it serves
   only `profiles/context/v<N>.json` for the URL the records use
   (`https://linked.art/ns/v1/linked-art.json`) and raises `UnpinnedContextError` for every other
   URL, so expansion never touches the network. The pinned v1 context has no `@import` and no
   remote scoped contexts, so nothing else had to be vendored (`backend/tests/rdf/test_context.py`
   asserts both).
2. **build**: the data graph from pyld's triples, either an rdflib `Graph` or an in-memory
   `pyoxigraph.Store` (direct term conversion; the alternatives are timed in section 3.4).
3. **validate**: pyshacl, in one of six configurations:

| Configuration | Shapes | `advanced` | Data store |
| :-- | :-- | :-- | :-- |
| `baseline` | `pyshacl.validate(g, shacl_graph=<path>)`: the Turtle file is parsed on every call | `True` (forces a copy of the data graph) | rdflib |
| `advanced_off` | Turtle file parsed on every call | `False` | rdflib |
| `preloaded_shapes` | parsed once into an rdflib `Graph`, passed to `pyshacl.validate`; pyshacl still harvests the shapes from it on every call | `False` | rdflib |
| `cached_shapes` | a `pyshacl.ShapesGraph` harvested once, reused by a `pyshacl.Validator` per record (no parsing or harvesting per call) | `False` | rdflib |
| `oxigraph` | as `preloaded_shapes` | `False` | pyoxigraph `Store` |
| `oxigraph_cached` | as `cached_shapes` | `False` | pyoxigraph `Store` |

pyshacl 0.40.2 accepts a `pyoxigraph.Store` as the data graph natively (its `DataGraph`
abstraction), and then runs SHACL-SPARQL constraints in oxigraph's own SPARQL engine. No
inference is used.

### 2.2 Method

- Every corpus `HumanMadeObject`, both subsets. For each record, the six configurations run
  interleaved (so background load falls on all of them alike): 5 untimed warm-up runs, then
  **50 timed runs**, after one untimed pass over the whole corpus per configuration.
- Percentiles are nearest-rank over all pooled samples (99 records x 50 runs = 4,950 per
  configuration). "Per-record median p95" is the p95 of the 99 per-record medians.
- **End-to-end** = expand + build + validate, the latency an API caller sees. The GO line is
  applied to end-to-end p95.
- Correctness checks: every configuration must report the same results per record (conformance
  and the multiset of `(lae:code, severity)`), must give the expected result for every broken
  record in `backend/tests/validation/data/hmo_shape_cases.json`, and a validator shared by
  8 threads must agree with the sequential run.

### 2.3 Shapes

Ten constraints from the Linked Art model documentation (`model/object/`, `model/base/`). Each
targeted shape carries one `lae:code`; the namespace and authoring rules are in
`profiles/shapes/README.md`.

| Code | Severity | Constraint | SHACL features |
| :-- | :-- | :-- | :-- |
| `LAE-SHACL-001` | Warning | At least one `Name` in `identified_by` (references exempt) | SPARQL |
| `LAE-SHACL-002` | Warning | If it has Names, one is classified as Primary Name | SPARQL |
| `LAE-SHACL-003` | Warning | At most one Primary Name per language | SPARQL (self-join) |
| `LAE-SHACL-004` | Violation | `produced_by` is at most one `Production` | `sh:maxCount`, `sh:class` |
| `LAE-SHACL-005` | Violation | Production and its parts `carried_out_by` a `Person` or `Group` | sequence + `sh:zeroOrMorePath`, `sh:or` of `sh:class` |
| `LAE-SHACL-006` | Violation | `made_of` is a `Material` | `sh:class` |
| `LAE-SHACL-007` | Violation | A `Dimension` has exactly one numeric `value` | `sh:targetObjectsOf`, counts, `sh:or` of `sh:datatype` |
| `LAE-SHACL-008` | Warning | A `Dimension` has one `unit` that is a `MeasurementUnit` | counts, `sh:class` |
| `LAE-SHACL-009` | Violation | `TimeSpan` bounds are `xsd:dateTime` | `sh:alternativePath`, `sh:datatype` |
| `LAE-SHACL-010` | Violation | `begin_of_the_begin` <= `end_of_the_end` | `sh:lessThanOrEquals` |

`LAE-SHACL-001` is a Warning, not a Violation, although the guide calls the Name "required": the
model documentation says "strongly RECOMMENDED", and 66 of the 99 corpus objects are documentation
snippets without a Name, which Step 1.9 must not block (it requires zero corpus violations).

## 3. Results

### 3.1 Latency per configuration

All 99 records, 50 timed runs each (4,950 samples per configuration), ms.

| Configuration | validate p50 | validate p95 | validate max | **end-to-end p50** | **end-to-end p95** | end-to-end p99 | end-to-end max | per-record median p95 | Results agree |
| :-- | --: | --: | --: | --: | --: | --: | --: | --: | :-- |
| `baseline` | 14.74 | 37.45 | 64.48 | 17.32 | **42.14** | 54.10 | 69.08 | 41.49 | yes |
| `advanced_off` | 14.70 | 36.68 | 61.60 | 17.17 | **41.29** | 51.95 | 65.55 | 41.20 | yes |
| `preloaded_shapes` | 12.81 | 33.88 | 55.72 | 15.18 | **38.17** | 44.26 | 58.98 | 38.28 | yes |
| `cached_shapes` | 12.50 | 33.57 | 56.59 | 14.90 | **37.74** | 50.08 | 74.78 | 38.38 | yes |
| `oxigraph` | 1.85 | 3.76 | 10.25 | 4.08 | **8.95** | 21.69 | 37.57 | 9.11 | yes |
| `oxigraph_cached` | 1.67 | 3.46 | 10.00 | 3.89 | **8.76** | 20.34 | 33.53 | 8.85 | yes |

Every configuration is under the 100 ms GO line at p95; the slowest single sample in the whole run
was 74.8 ms.

### 3.2 Stage breakdown

ms, p50 / p95, pooled.

| Configuration | expand (pyld) | build data graph | validate (pyshacl) |
| :-- | --: | --: | --: |
| `baseline` | 2.22 / 7.39 | 0.09 / 0.61 | 14.74 / 37.45 |
| `advanced_off` | 2.21 / 7.07 | 0.09 / 0.60 | 14.70 / 36.68 |
| `preloaded_shapes` | 2.19 / 6.95 | 0.09 / 0.59 | 12.81 / 33.88 |
| `cached_shapes` | 2.18 / 6.91 | 0.09 / 0.59 | 12.50 / 33.57 |
| `oxigraph` | 2.07 / 6.55 | 0.05 / 0.34 | 1.85 / 3.76 |
| `oxigraph_cached` | 2.08 / 6.49 | 0.05 / 0.34 | 1.67 / 3.46 |

### 3.3 Slowest records

Median of 50 runs (and p95 for the recommended configuration), ms. The five institutional records
are the largest and the slowest; on them pyld expansion is most of the time.

| Record | Subset | Triples | `oxigraph_cached` end-to-end | `oxigraph_cached` end-to-end p95 | `oxigraph_cached` validate | `baseline` end-to-end |
| :-- | :-- | --: | --: | --: | --: | --: |
| `ycba-object-5497f1dd-…` | institutional | 316 | 23.31 | 32.36 | 4.23 | 40.13 |
| `ycba-object-f13960d7-…` | institutional | 261 | 13.63 | 18.64 | 2.89 | 37.21 |
| `ycba-object-71b6123e-…` | institutional | 255 | 12.41 | 17.16 | 2.10 | 40.83 |
| `yuag-obj-31619.json` | institutional | 195 | 10.84 | 15.92 | 3.10 | 33.99 |
| `yuag-obj-360556.json` | institutional | 176 | 8.85 | 9.32 | 1.87 | 22.04 |
| `object-2.json` | linkedart | 26 | 7.89 | 11.64 | 4.12 | 41.49 |
| `object-0.json` | linkedart | 110 | 7.65 | 8.07 | 2.82 | 31.12 |
| `object-gradualpage-1.json` | linkedart | 13 | 6.96 | 7.39 | 3.83 | 42.32 |

### 3.4 Data graph preparation

Each route timed on its own, 50 runs per record, pooled, ms.

| Route | p50 | p95 | max |
| :-- | --: | --: | --: |
| pyld `to_rdf` to a dataset | 1.86 | 6.61 | 19.53 |
| pyld `to_rdf` to N-Quads text | 1.87 | 6.74 | 20.02 |
| dataset to rdflib `Graph` | 0.06 | 0.60 | 10.03 |
| N-Quads text to rdflib `Graph` (parse) | 0.10 | 1.07 | 11.51 |
| dataset to pyoxigraph `Store` | 0.03 | 0.35 | 0.90 |
| N-Quads text to pyoxigraph `Store` (`Store.load`) | 0.02 | 0.16 | 0.27 |

### 3.5 Scale probe

The corpus objects are small (mean 29 triples, largest 316). To see where the budget runs out, the
largest record (`ycba-object-5497f1dd-…`) was inflated by repeating every embedded (id-less) array
item of the record and of its Production, 20 runs per size, ms.

| Factor | Triples | expand p50 | `baseline` validate p50 | `baseline` end-to-end p50 / p95 | `cached_shapes` validate p50 | `cached_shapes` end-to-end p50 / p95 | `oxigraph_cached` validate p50 | `oxigraph_cached` end-to-end p50 / p95 |
| --: | --: | --: | --: | --: | --: | --: | --: | --: |
| 1 | 316 | 13.43 | 29.08 | 44.52 / 54.62 | 24.54 | 39.32 / 49.50 | 3.37 | 17.41 / 23.81 |
| 2 | 513 | 23.26 | 30.84 | 58.52 / 68.74 | 26.61 | 57.08 / 61.99 | 3.77 | 27.82 / 34.89 |
| 4 | 907 | 43.55 | 36.50 | 91.74 / 110.96 | 29.15 | 83.21 / 86.38 | 4.47 | 47.99 / 55.57 |
| 8 | 1,695 | 86.86 | 45.56 | 141.12 / 151.97 | 36.57 | 130.79 / 140.11 | 6.27 | 96.41 / 133.53 |
| 16 | 3,271 | 176.58 | 85.02 | 287.60 / 299.14 | 69.00 | 259.23 / 309.68 | 9.90 | 182.22 / 188.01 |

With the pyoxigraph store, end-to-end time grows by about 0.055 ms per triple, and pyld expansion
is 90 % of it from 900 triples up; pyshacl itself stays under 10 ms at 3,271 triples.

### 3.6 Shape formulation

Each shape validated on its own (`use_shapes`, harvested shapes graph) on the largest record and on
its 8x inflation, median of 20 runs, ms.

| Data graph | 001 | 002 | 003 | 004 | 005 | 006 | 007 | 008 | 009 | 010 |
| :-- | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: |
| 316 triples, rdflib | 7.17 | 5.52 | 10.17 | 0.14 | 0.27 | 0.14 | 0.52 | 0.16 | 0.16 | 0.13 |
| 316 triples, pyoxigraph | 0.58 | 0.68 | 1.07 | 0.13 | 0.22 | 0.13 | 0.49 | 0.16 | 0.17 | 0.14 |
| 1,695 triples, rdflib | 8.56 | 6.27 | 18.31 | 0.14 | 0.32 | 0.14 | 2.54 | 0.28 | 0.16 | 0.14 |
| 1,695 triples, pyoxigraph | 0.59 | 0.76 | 1.32 | 0.15 | 0.30 | 0.14 | 2.63 | 0.37 | 0.19 | 0.15 |

The three SHACL-SPARQL shapes (001 to 003) are the only expensive ones on rdflib, and they are
cheap on pyoxigraph. `LAE-SHACL-001` and `-002` were first written in SHACL Core: `sh:or` over a
closed "reference stub" shape (`sh:closed true; sh:ignoredProperties (rdf:type rdfs:label)`) and
over qualified value shapes. The benchmark keeps that version (`CORE_OR_SHAPES`) for comparison:

| Data graph | 001 SPARQL (shipped) | 001 Core `sh:or` | 002 SPARQL (shipped) | 002 Core `sh:or` |
| :-- | --: | --: | --: | --: |
| 316 triples, rdflib | 7.17 | 3.30 | 5.52 | 0.69 |
| 316 triples, pyoxigraph | 0.58 | 7.16 | 0.68 | 1.57 |
| 1,695 triples, rdflib | 8.56 | 19.90 | 6.27 | 3.21 |
| 1,695 triples, pyoxigraph | 0.59 | **49.79** | 0.76 | 11.12 |

Both versions gave the same results on the corpus and on the broken-record cases.

### 3.7 Correctness checks

- All six configurations report identical results for all 99 records.
- All 12 broken-record cases give exactly the expected `(lae:code, severity)` results in all six
  configurations: one case per code (two for `-003`) and a valid record that must produce nothing.
  The valid record carries a `part_of` reference to another `HumanMadeObject`, which must not be
  flagged.
- 495 validations (each record 5 times) on 8 threads sharing one harvested `ShapesGraph`, with
  pyoxigraph stores: 0 disagreed with the sequential results.
- The golden corpus has **zero violations**. The warnings are listed in Appendix A.

## 4. Findings

1. **Every configuration is a GO**, including the naive one (Turtle parsed per call,
   `advanced=True`, rdflib): end-to-end p95 42.1 ms, max 69.1 ms. The recommended configuration is
   almost five times faster at p95 (8.8 ms), and ten times faster in pyshacl alone (3.5 ms).
2. **A pyoxigraph data store is the change that matters.** pyshacl's validate time drops from 12.5
   to 1.7 ms at p50 and from 33.6 to 3.5 ms at p95 (`cached_shapes` against `oxigraph_cached`). The
   gain comes from SHACL-SPARQL: on a pyoxigraph store pyshacl runs those queries in oxigraph
   rather than in rdflib's SPARQL engine (section 3.6: 5.5 to 18 ms per SPARQL shape on rdflib, 0.6
   to 1.3 ms on oxigraph). Loading N-Triples into a `Store` is also the cheapest way to build the
   data graph (0.02 ms p50).
3. **Pre-loading the shapes saves about 2 ms per call; the other options are noise.** Parsing the
   shapes once (`preloaded_shapes`) saves 1.9 ms at p50 and 2.8 ms at p95 of validate time against
   parsing the Turtle on every call. Reusing a harvested `ShapesGraph` saves another 0.2 to 0.3 ms,
   but needs pyshacl internals (subclassing `Validator`, calling `pyshacl.monkey.apply_patches` and
   `pyshacl.validator.assign_baked_in` once). `advanced=False` saves under 1 ms (it avoids copying
   the data graph); the shapes use no SHACL-AF feature, so turning it off costs nothing.
4. **pyld expansion, not SHACL, sets the per-record cost.** With the recommended configuration,
   expansion is about half of the corpus end-to-end time and 90 % of it above 900 triples (section
   3.5). The 100 ms line is crossed at about 1,700 triples, and pyld is 87 of those 96 ms. That is
   Step 1.3's concern; Step 1.9 should expand each record once per validation request and share
   the triples between the Python rules and SHACL.
5. **How a shape is written matters more than how pyshacl is configured.** pyshacl evaluates every
   member of an `sh:or`, and for every member that fails it builds a full result, including a text
   rendering of each blank node involved, then throws it away. A closed shape fails once per extra
   property, so `sh:or` over `sh:closed` cost 49.8 ms on the 1,695-triple record, against 0.6 ms
   for the same rule in SHACL-SPARQL on pyoxigraph (section 3.6). The shipped shapes use
   SHACL-SPARQL for the three cross-node rules, and `profiles/shapes/README.md` records the rule
   for later shape authors, including the shapes Phase 2 Step 2.2 generates from Value Sets.
6. **SHACL is the wrong place for `xsd:dateTime` checks.** Their latency is fine (under 0.2 ms
   each), but pyshacl 0.40.2 with rdflib 7.6.0 mishandles datetimes it cannot turn into a Python
   `datetime`, on both data stores:
   - an ill-typed bound (`"1642"`, which the context types as `xsd:dateTime`) makes
     `sh:lessThanOrEquals` (`LAE-SHACL-010`) raise `TypeError: '<=' not supported between
     instances of 'Literal' and 'Literal'`. That aborts the whole validation, so no other result is
     reported either;
   - a BCE bound (`-0500-01-01T00:00:00Z`, a valid `xsd:dateTime`) is reported as a violation of
     `sh:datatype xsd:dateTime` (`LAE-SHACL-009`), and `-010` raises the same `TypeError` for some
     BCE pairs and silently passes others.

   Valid CE datetimes, with or without a timezone, compare correctly. The two defects are pinned by
   `xfail(strict=True)` tests in `backend/tests/validation/test_hmo_shapes.py`, so a pyshacl upgrade
   that fixes them is noticed. JSON Schema runs first, but `jsonschema` checks the vendored schema's
   `format: date-time` only when a format checker is enabled, and RFC 3339 then rejects BCE years
   as well. **Recommendation for Step 1.9:** implement `TimeSpan` datetime validity and ordering
   in the Python rule `LAE-RULE-001` (which the guide already plans), comparing ISO 8601 values
   with signed years, and drop `LAE-SHACL-009` and `-010` from the shapes. The question of BCE
   support is in `docs/questions.md` (Step 0.9).
7. **Threads give no speed-up.** 495 validations took 2.26 s sequentially and 2.61 s on 8 threads:
   the work is Python under the GIL. Sharing one shapes graph across threads is safe (0
   mismatches). `asyncio.to_thread` keeps the event loop free for an interactive validation; batch
   validation (import wizard, agent sweeps) needs a process pool to use more than one core.
8. **pyshacl's `conforms` is `False` whenever there is a warning**, unless `allow_warnings=True` is
   passed: 72 of the 99 corpus objects "do not conform", yet none has a violation. Step 1.9 maps
   `sh:resultSeverity` itself; `Diagnostics.ok` must be computed from the mapped severities, not
   from `conforms`.
9. **A compiled shape evaluator is not worth building now** (`docs/implementation_phases.md` lists
   it as an alternative to evaluate). pyshacl on pyoxigraph spends 1.7 ms (p50) and 3.5 ms (p95)
   per corpus record, under 10 ms at 3,271 triples, and less than pyld expansion at every size
   measured, so a compiled evaluator could save a few milliseconds at most.

## 5. GO / NO-GO

**GO.** End-to-end p95 per record over the whole corpus is 8.76 ms with the recommended
configuration and 42.14 ms with the slowest one, against a line of 100 ms. No constraint has to
move to the Python rule engine for latency. For correctness, `LAE-SHACL-009` and `-010` should move
to `LAE-RULE-001` (finding 6).

## 6. Recommended per-record budget

**50 ms per call of `validate_shacl`** (Step 1.9: expansion, data graph, and pyshacl), logged and
continued when exceeded, as the guide says. On this machine:

- the corpus takes 3.9 ms at p50 and 8.8 ms at p95. Its slowest record takes 23.3 ms (median) and
  32.4 ms (p95), so 50 ms leaves at least 1.5x headroom for every corpus record at p95;
- 50 ms is reached at about 900 triples (48 ms median at 907 triples), three times the largest
  corpus object. Beyond that, cost grows with pyld expansion, about 0.055 ms per triple;
- the pyshacl call alone should stay under 10 ms up to about 3,000 triples. A slower call points
  to an expensively written shape (finding 5).

CI runners and production hosts may be slower than an M5 Max, so the budget is a logging
threshold, not a test assertion.

## 7. How Step 1.9 should call pyshacl

```python
import pyoxigraph as ox
import pyshacl
from rdflib import Graph

# Once, at startup: every profiles/shapes/*.ttl in one rdflib Graph.
SHAPES = Graph()
for path in sorted(SHAPES_DIR.glob("*.ttl")):
    SHAPES.parse(path, format="turtle")

# Per record, in asyncio.to_thread: the canonical N-Triples from expand_to_ntriples, plus one
# rdf:type triple per referenced entity, loaded into an in-memory pyoxigraph Store.
store = ox.Store()
store.load(ntriples, format=ox.RdfFormat.N_TRIPLES)
_, report, _ = pyshacl.validate(store, shacl_graph=SHAPES, advanced=False, inference="none")
# report is an rdflib Graph: read sh:sourceShape -> lae:code, sh:resultSeverity, sh:focusNode,
# sh:resultPath and sh:value from each sh:ValidationResult.
```

This is the `oxigraph` configuration. It is 0.2 ms slower at p95 than reusing a harvested
`ShapesGraph`, and it uses only pyshacl's public API, so it should survive pyshacl upgrades. Catch
any exception from `pyshacl.validate` and report it as a validation failure rather than a 500
(finding 6 shows one way it raises). Everything here was measured with pyshacl 0.40.2, the version
in `backend/uv.lock`.

## Appendix A. Corpus diagnostics (warnings only)

| Code | Results | Where |
| :-- | --: | :-- |
| `LAE-SHACL-001` | 66 | 64 record roots without a `Name` (all linked.art examples), and two embedded objects without one: `object-2.json` `part/0` and `yuag-obj-31619.json` `used_for/used_specific_object/0` |
| `LAE-SHACL-002` | 7 | Names, but none classified as Primary Name: `object-flw_house-1`, `object-spring-8` to `object-spring-13` |

Two more linked.art examples have no Name and are not reported: `object-nightwatch-50.json` and
`object-spring-1.json` consist of `type` and `_label` only, which `LAE-SHACL-001` treats as a
reference rather than a description. A draft with nothing but a type and a label is likewise not
warned about a missing Name until it gains another property.

## Appendix C. Step 1.9 follow-up (Phase 1)

Step 1.9 adopted the recommendations: pyshacl on a pyoxigraph store with the shapes loaded once
and `advanced=False`; severities read from the results; `LAE-SHACL-009` and `-010` retired in
favour of the Python rules `LAE-RULE-005` and `LAE-RULE-001`; a 50 ms budget that is logged; a
process pool for batches. The shapes grew to 19 (`common.ttl`, `Person.ttl`, `Place.ttl`,
`Type.ttl`; codes up to `LAE-SHACL-021`). The benchmark keeps the ten shapes it measured, as
`scripts/spikes/s4_shacl/HumanMadeObject.s4.ttl` and `hmo_shape_cases.s4.json`.

Two things differ from Appendix A. JSON Schema now runs first, so the 60 files in
`tests/corpus/KNOWN_FAILURES.md` stop there and never reach SHACL. And a referenced entity is no
longer a focus node, so only nodes of the record are reported.

**Corpus result (244 files):** 60 fail JSON Schema, exactly the known failures. The other 184
have **zero violations** from SHACL and the Python rules, and these warnings
(`backend/tests/validation/test_engine.py` pins the counts):

| Code | Results | Where |
| :-- | --: | :-- |
| `LAE-SHACL-001` | 46 | 45 object roots without a `Name`, and the embedded object at `members_exemplified_by/0` of `set-exhset-2.json` |
| `LAE-SHACL-002` | 7 | Objects with Names, none classified as Primary Name |
| `LAE-SHACL-014` | 1 | A `TimeSpan` without both outer bounds: `carried_out/0/timespan` of `yuag-agt-67740.json` |
| `LAE-SHACL-015` | 18 | `Person` roots without a `Name` |
| `LAE-SHACL-018` | 2 | `Place` roots without a `Name` |
| `LAE-SHACL-020` | 3 | `Type` roots without a `Name` |

No shape or rule had to be changed for the corpus. Whole-pipeline latency per record (schema,
rules, expansion, SHACL, path mapping) over those 184 records on the same machine, one run with
other agents sharing it: 2.7 ms p50, 11.2 ms p95, 23 ms max.

## Appendix B. Reproducing

```sh
uv run --project backend python scripts/spikes/s4_shacl/bench.py \
    --iterations 50 --warmup 5 --out /tmp/lae-i9/s4-results.json
cd backend && uv run pytest tests/rdf tests/validation -q
```

The benchmark took 11 minutes on this machine. `--config NAME` (repeatable) limits it to some
configurations; `--scale` and `--scale-iterations` control the scale probe.

## Files

| Path | What |
| :-- | :-- |
| `backend/src/lae/rdf/context.py` | `load_context(version)`, the offline `document_loader`, `UnpinnedContextError` |
| `backend/tests/rdf/test_context.py` | The loader serves only pinned contexts and never opens a socket |
| `profiles/shapes/HumanMadeObject.ttl` | The shapes (eight since Step 1.9; the ten the spike measured are `scripts/spikes/s4_shacl/HumanMadeObject.s4.ttl`) |
| `profiles/shapes/README.md` | The `lae:` and `laes:` namespaces, `lae:code`, authoring rules, code table |
| `backend/tests/validation/test_hmo_shapes.py`, `backend/tests/validation/data/hmo_shape_cases.json` (replaced in Step 1.9 by `test_shacl.py` and `data/shape_cases.json`; the spike's cases are `scripts/spikes/s4_shacl/hmo_shape_cases.s4.json`) | `lae:code` rules, one broken record per code on both stores, zero corpus violations, the two pinned pyshacl defects |
| `scripts/spikes/s4_shacl/bench.py` | The benchmark |
