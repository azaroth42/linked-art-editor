# Reuse Map: Adjacent Repositories

Surveyed November 2026. All paths are absolute on the development machine. These repositories are read-only for this project; copy code into `backend/src/lae/` with a `# Vendored from <repo>/<path> @ <commit>` header and add tests here.

Legend: **Vendor** = copy near-verbatim. **Pattern** = re-implement following the same design. **Avoid** = do not reuse.

---

## linked-art/linked.art — `/Users/rs2668/Development/linked-art/linked.art` (commit 7650a86)

The human-readable model documentation and the normative examples.

| Use | Path | Notes |
| :-- | :-- | :-- |
| Vendor | `docs/example/<category>/*.json` (211 files) | The golden corpus. Copied by `scripts/sync_corpus.py` (Phase 0, Step 0.4). |
| Vendor | `docs/ns/v1/linked-art.json` | The pinned `@context`, copied to `profiles/context/v1.json`. Also `record-types.json` lists the top-level classes. |
| Reference | `docs/model/**` | Property and class names for every UI Profile and SHACL shape. When a guide says "per the Linked Art model", this is where to look. |
| Reference | `docs/api/**` | The Linked Art API: which classes are top-level records and what a reference stub looks like. |
| Pattern | `scripts/aat_labels.json`, `scripts/hal_links.json` | AAT label map used by the docs build; a seed for `entity_labels` labels of commonly used AAT terms. |

## linked-art/json-validator — `/Users/rs2668/Development/linked-art/json-validator` (commit 43277cc)

| Use | Path | Notes |
| :-- | :-- | :-- |
| Vendor | `schema/*.json` | Draft 7 JSON Schema. `core.json` and `linked_art.json` are shared; `object.json`, `person.json`, `group.json`, `place.json`, `concept.json`, `set.json`, `digital.json`, `text.json`, `image.json`, `event.json`, `provenance.json`, `abstract.json` are per-class entry points. Copied to `profiles/schema/` (Phase 0, Step 0.4). |
| Pattern | `schema-test.py` | `Draft7Validator.iter_errors` usage and the error path formatting that `lae.validation.schema` reproduces as `Diagnostics`. |
| Pattern | `lux-schema-test.py` | Shows how LUX extends the schema; the model for institution-specific schema overlays later. |

## linked-art/linkedart-parser — `/Users/rs2668/Development/linked-art/linkedart-parser`

Natural-language-to-Linked-Art FastAPI service built on Gemini. Small (about 2,100 lines).

| Use | Path | Notes |
| :-- | :-- | :-- |
| Vendor | `app/builder.py` | Deterministic Linked Art assembler with verified AAT constants (`PRIMARY_NAME`, `TYPE_OF_WORK`, `DESCRIPTION`, provenance event types), node helpers `_name`, `_identifier`, `_statement`, `_timespan`, and `Builder` methods for every record shape. Becomes `lae/builder.py`, the basis of the MCP draft-authoring tools (Phase 3) and Smart Paste (Phase 4). Replace the `Minter` with UUIDv7 minting and drop `_label` on stubs per §3.2. |
| Vendor | `app/schemas.py` | `EXTRACTION_SCHEMAS` and `FIELD_KINDS` for objects, digital content, collections, provenance, exhibitions, conservation. The spec for prose-to-draft extraction in Smart Paste. |
| Pattern | `app/reconcile.py`, `app/gemini.py`, `app/pipeline.py` | The rule "the LLM extracts strings and selects among candidates; it never mints URIs", plus the audit trail shape (`extraction`, `candidates`, `selections`). Reuse the rule; replace live Getty and Wikidata calls with local sidecar search. |
| Vendor (data) | `data/aat_dataset.csv` | 59k AAT concepts, columns `aat_code,preferred_name,description`. Seeds `entity_labels` for AAT in Phase 1 before the full AAT sidecar arrives in Phase 2. |
| Pattern | `app/aat_local.py` | SQLite FTS5 prefix search with term normalization and qualifier stripping. The normalization rules carry over to autocomplete. |
| Avoid | `app/main.py`, templates, Vertex credentials | Service wiring specific to that deployment. |

## lux/qlever-middletier — `/Users/rs2668/Development/lux/qlever-middletier`

LUX's read-only middle tier on QLever with LMDB record storage. It never writes to QLever.

| Use | Path | Notes |
| :-- | :-- | :-- |
| Vendor | `qleverlux/sparql/terms.py` (`iri`, `string_literal`) and `qleverlux/sparql/builder.py` with its tests | Injection-safe SPARQL term escaping and a query builder. Add `INSERT DATA` / `DELETE DATA` / `DELETE WHERE` builders (their `TODO.md` §5.1 sketches this). Becomes `lae/rdf/sparql.py`. |
| Pattern | `qleverlux/clients/qlever.py` | httpx HTTP/2 client posting `query` with `Accept: application/qlever-results+json`, 30 s timeout, 2 s connect timeout, load shedding at 64 in-flight requests, `process_qlever_results` literal coercion. **Drop the `alru_cache`**: an editor reads after it writes. Becomes `lae/sync/qlever_client.py`. |
| Vendor | `sparql_service/sparql.py`, pattern from `sparql_service/app.py` (`VectorService`) and `sparql_service/README.md` | A working SPARQL `SERVICE` endpoint that QLever federates to: parses the pushed-down `VALUES` block, exposes `vec:similarTo`, `vec:score`, `vec:rank`, `vec:topK`, `vec:mode auto|constrained|global`, graph-first vs vector-first modes, returns `application/sparql-results+json`. Port to pgvector in Phase 3; keep the request contract. Known pitfalls: QLever has no SERVICE timeout (the service enforces 30 s and returns 503); above `service-max-value-rows` (default 10k) QLever silently pushes nothing, so raise it and use `vec:mode "constrained"`; duplicate SERVICE calls are not shared. |
| Vendor | `lux_embed/encoder.py`, `lux_embed/linkedart.py` | Sentence-transformers encoder (EmbeddingGemma 2 or Harrier) and the extraction of names and statements from Linked Art records into embedding "cards". Basis of the Phase 3 embedding worker. Keep torch out of the `api` venv (`tests/test_import_boundaries.py` is the pattern). |
| Pattern | `files/Qleverfile`, `files/Qleverfile.build`, `installing_qlever.md` | `TEXT_INDEX = from_literals`, `ADD_HAS_WORD_TRIPLES = true`, memory and thread settings, materialized `<scope>Words` views, and the raw `IndexBuilderMain` command. The LUX build uses the `project-lux/qlever` fork. No `--persist-updates` there; add it. |
| Pattern | `qleverlux/query/text.py` | Name search via `ql:has-word` inside `GRAPH`, any-text via `SERVICE view:<scope>Words`, phrases as `CONTAINS`. Note: no prefix or autocomplete query exists; view weights are fixed at index time. |
| Pattern | `qleverlux/query/related.py` | Backlink and HAL count queries (`OPTIONAL { SELECT ?uri (COUNT(?what) …) }` with `LIMIT 1` probes). Expensive; cache them. |
| Pattern | `qleverlux/app.py` (`MiddleTier` lifespan, `Settings`) | Service composition in the FastAPI lifespan. |
| Avoid | `qleverlux/query/translator.py`, `queries/*.json`, `config/`, `services/hal.py`, `services/cms.py` | Bound to luxql and the flattened `lux:` vocabulary. |

Lessons recorded in their `CLAUDE.md`: wrong predicate names fail silently as empty results (count triples first); QLever sorts with ICU collation; raw text scores range widely and need per-query normalization; round paging offsets so the cache hits.

## lux/data-pipeline — `/Users/rs2668/Development/lux/data-pipeline`

LUX's batch ingestion: fetch, map, reconcile, identify, merge, export. Reconciliation is exact-key LMDB lookup; there is no service and no scoring.

| Use | Path | Notes |
| :-- | :-- | :-- |
| Vendor | `lux_pipeline/storage/idmap/lmdb.py` (`TabLmdb`) | LMDB key-value wrapper used for all reconciliation indexes. |
| Vendor | `lux_pipeline/process/base/reconciler.py` (`LmdbReconciler.reconcile`, `extract_names`) | Name-variant extraction (person "last, first", birth/death year variants, place variants) and exact name/URI lookup with type checks. Core of the Phase 3 `lae-reconcile` process, extended to return *candidates with evidence* instead of a single silent hit. |
| Vendor | `lux_pipeline/process/identity_resolver.py` (`DSU`, `_constrained_union`) | Union-find that refuses unions across a differentFrom pair. Pattern for the negative cache check before any merge. |
| Vendor | `lux_pipeline/process/merger.py` (`RecordMerger.merge_common` and per-class functions) | Field-level merge rules (first value wins for singletons, lists deduplicated). Basis of the default `field_resolution_plan` in Phase 2 merges. Needs a small stub for `configs`/`idmap`. |
| Vendor | `lux_pipeline/sources/lux/qlever/mapper2.py` (`clean_iri`, `clean_datetime`, `pad_datetime`) | Literal and IRI sanitation; one bad literal silently truncates a QLever index build. Apply to every N-Triples export. |
| Pattern | `lux_pipeline/sources/getty/fetcher.py`, `getty/harvester.py` | Getty serves Linked Art directly from `data.getty.edu`; records are fetched one at a time and kept current by harvesting the ActivityStreams feed. The Phase 2 sidecar manager for AAT, ULAN, TGN does the same, with the SSRF egress filter. |
| Pattern | `lux_pipeline/sources/wikidata/loader.py`, `make_wikidata_lmdb.py` | Streaming the Wikidata dump and writing the typed-key LMDB store plus N-Triples. The existing LUX Wikidata subset store is the Phase 2 Wikidata sidecar source. |
| Pattern | `lux_pipeline/process/reference_manager.py` (`walk_for_refs`, `merge_metatypes`), `globals.json` | Metatypes are collected from `classified_as` on embedded `Type` references (nationality 300379842, occupation 300263369, style 300015646, gender 300055147). Same approach fills `entity_labels.metatype_uris`. |
| Pattern | `lux_pipeline/process/update_manager.py`, `experiment-incremental-update.py` | Lessons from the unfinished incremental update: equivalence changes can merge or split entities, and deletes are the hard part. Confirms the design's explicit merge and split batches. |
| Pattern | `lux_pipeline/process/sheets.py`, `sources/lux/final/index_loader.py` (`global_diffs`, `global_equivs`) | Curator block and allow lists. The negative cache in `curation_proposals` plays the same role. |
| Avoid | `lux_pipeline/cli/build.py`, `process/phases/*`, ray/mp engines, MarkLogic mappers | Whole-build orchestration; not applicable to an incremental editor. |
| Avoid | `sources/lux/qlever/mapper2.py` as an RDF exporter | It writes a lossy `lux:` search projection, not canonical Linked Art triples. The editor expands JSON-LD with pyld and the pinned context. |

## lux/qlever-dev — `/Users/rs2668/Development/lux/qlever-dev` (upstream `ad-freiburg/qlever` master, commit ba7b74cc)

The QLever source the project builds from. It is upstream master, not the older `project-lux` fork referenced by the LUX middle tier's install notes.

| Use | Path | Notes |
| :-- | :-- | :-- |
| Reference | `CMakeLists.txt`, `README.md` build notes | How to build QLever natively on Linux when no package is available. The development machine uses Homebrew `qlever` 0.6.0. Record the binary version in `deploy/QLEVER_VERSION` and which binary each spike used. No Docker images (ADR-0017). |
| Verify | `src/index/DeltaTriples.cpp` (around lines 236–250) | Updates add the same extra triples as the index build for language tags and `ql:has-word` when the index was built with `ADD_HAS_WORD_TRIPLES`. This is why Spike S1 tests `ql:has-word` on inserted literals; `ql:contains-word` prefix search over updated literals is still a measured question. |
| Verify | `src/index/IndexImpl.cpp` (`persistUpdates`, around lines 1135 and 1210) | The persistence path behind `--persist-updates`. |
| Reference | `src/index/IndexBuilderMain.cpp` (`text-index` options), `e2e/`, `examples/` | Index build flags and end-to-end query examples. |
| Reference | `README.md`, `misc/` | Qleverfile format and server flags. Do not vendor code from here; QLever is consumed as a binary. |

## lux/dataset — `/Users/rs2668/Development/lux/dataset/jsonl/`

The full LUX export: gzipped JSONL files (`export_full_*.jsonl.gz`), one merged Linked Art record per line, with LUX URIs.

| Use | Path | Notes |
| :-- | :-- | :-- |
| Data | `export_full_*.jsonl.gz` | Primary source for the 1M (Step 1.22) and 10M (Step 2.20) scale runs and blue/green rebuild timing, replacing synthetic records where possible. Stream with `gzip`; never load whole files into memory. Verify each file with `gzip -t` first. Sample corpus additions deterministically (fixed seed) and record provenance in `tests/corpus/SOURCE.md`. |

## llm/chai-engine — `/Users/rs2668/Development/llm/chai-engine`

LLM workflow engine built on LiteLLM. Synchronous and in-memory; no job persistence, queues, or key management.

| Use | Path | Notes |
| :-- | :-- | :-- |
| Vendor as functions | `chai/ai/llm.py` (`build_base_kwargs`, `response_format`, `image_part`, `get_usage`, `get_cost`, `extract_text`, `embed`) | Request construction and reply handling for every provider via LiteLLM. Copy the functions into `lae/llm/adapter.py`; **never import the `chai` package** (it pulls torch and ultralytics and loads `.env` at import). |
| Vendor | `chai/ai/ai_utils.py` | JSON fence and truncation repair, prompt-order cache warnings. Change `extract_json` to raise instead of returning `{}`. |
| Vendor | `chai/usage.py` (`UsageLedger`) | Thread-safe per-model token and cost tally; feeds `agent_jobs` and `agent_audit_log` cost columns. |
| Pattern | `chai/ai/gemini.py` | Vertex wiring and version-dependent thinking settings. |
| Pattern | `chai/persistence/rows.py` (`correction_row`), `chai_tea_schema.sql` | Append-only correction rows with `previous_id`, `editor_user_id`, and agent metadata. Model for audit rows and for AttributeAssignment provenance on model output. Do not share its table names. |
| Pattern | `chai/decision.py`, `examples/reconcile_wikidata.py`, `chai/system_one/base.py` (`gliner-decide-linkedart-recon`) | Candidate selection as a scored decision step. |
| Avoid | `chai/core.py`, `chai/workflow.py`, `chai/iterator.py` as the `agent_jobs` runner | Synchronous, thread-shared component instances, no resume, cancel, priority, or persistence. |
| Avoid | `chai/translator.py` `GlossaryTranslator` | A regex term swap, not prompt-level glossary injection. |

Operational lesson: `litellm` is pinned to an exact version there because release 1.107.0 was compromised. The editor pins its own exact version and reviews upgrades.

---

## Findings that changed the design

1. **Reconciliation service.** There is no LUX reconciliation service to wrap. `lae-reconcile` is a new small process that loads the LUX LMDB indexes with network access off and returns ranked candidates with evidence, paired with lexical and semantic candidate search for fuzzy suggestions. Recorded in spec §6.3 and §14.
2. **Vector SPARQL SERVICE.** A tested implementation already exists in `qlever-middletier/sparql_service`. Spec §14 Open Question 1 and ADR-0016 record this; Phase 3 ports it to pgvector.
3. **QLever text freshness.** LUX text search depends on index-time views and the `from_literals` text index, and the middle tier never writes. The local `qlever-dev` source adds `ql:has-word` triples for updated literals, which is encouraging, but Spike S1 must still prove prefix search freshness after `INSERT DATA` before autocomplete relies on it.
4. **Canonical triples.** No adjacent repo expands Linked Art JSON-LD to canonical triples; `lae.rdf` builds this with pyld and the pinned context.
