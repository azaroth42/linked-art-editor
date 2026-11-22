# Phase 0 — Foundations & Risk Spikes: Step-by-Step Guide

**Read first:** `docs/README.md`, `docs/conventions.md`, `docs/glossary.md`, `docs/spec/` (section numbers are preserved from the original design document) sections 2, 3, 4, and `docs/reuse_map.md`. The human-readable Linked Art model documentation is checked out locally at `/Users/rs2668/Development/linked-art/linked.art/docs/model/` and is the reference for every class and property name used in this project.
**Goal:** A buildable monorepo with CI, the core database schema, the golden test corpus, and written go/no-go results for six technology spikes.
**You are done when** every item in the Exit Checklist at the bottom passes.

Rules for this phase:
- Do not build UI features. Do not build the editor. This phase proves the foundations only.
- Every spike ends with a markdown report in `docs/spikes/`. A spike without a report is not finished.
- If a spike fails, do not redesign anything. Write the result and the fallback named in `docs/implementation_phases.md`, then stop and ask a human.

---

## Step 0.1 — Create the monorepo skeleton

**Why:** Every later phase assumes this exact layout.

**Do:**
1. Create these directories and placeholder files:
   ```
   backend/                 Python service (package name: lae)
     pyproject.toml
     src/lae/__init__.py
     src/lae/api/__init__.py
     src/lae/crdt/__init__.py
     src/lae/projection/__init__.py
     src/lae/rdf/__init__.py
     src/lae/sync/__init__.py
     src/lae/validation/__init__.py
     src/lae/profiles/__init__.py
     src/lae/vocab/__init__.py
     src/lae/db/__init__.py
     src/lae/auth/__init__.py
     src/lae/cli.py
     tests/__init__.py
     tests/conftest.py
   frontend/                React 19 + TypeScript SPA
     package.json
     src/main.tsx
     src/app/
     src/editor/
     src/widgets/
     src/crdt/
     src/search/
     src/reader/
     src/i18n/
     src/admin/
     public/locales/en/common.json
   profiles/                Declarative config, versioned with the code
     ui/                    UI Profiles (YAML), one file per Linked Art class
     shapes/                SHACL shapes (Turtle)
     vocab/                 Value Set definitions (YAML)
     context/               Pinned Linked Art @context JSON files, one per version
     schema/                Linked Art JSON Schema (vendored from linked-art/json-validator)
   deploy/
     Qleverfile             QLever config (native)
     Procfile               development process list
     systemd/               production unit templates
     Qleverfile
     postgres/init.sql
   scripts/
   docs/                    (already exists)
   tests/corpus/            Golden corpus JSON-LD files (see Step 0.4)
   ```
2. `backend/pyproject.toml`: project name `lae`, `requires-python = ">=3.12"`, build backend `hatchling`, dependencies:
   `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`, `sqlalchemy[asyncio]>=2`, `asyncpg`, `alembic`, `pycrdt`, `pycrdt-websocket`, `pyoxigraph`, `pyld`, `pyshacl`, `jsonschema`, `httpx[http2]`, `zstandard`, `uuid-utils` (for UUIDv7), `authlib`, `python-multipart`, `orjson`.
   Dev dependencies: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`, `hypothesis`. No Docker or `testcontainers`: tests use a native PostgreSQL via `LAE_TEST_DATABASE_URL`.
   Tooling: `[tool.ruff]` line-length 100, `select = ["E","F","I","B","UP","ASYNC"]`; `[tool.mypy]` `strict = true`, `plugins = ["pydantic.mypy"]`; `[tool.pytest.ini_options]` `asyncio_mode = "auto"`.
3. Use `uv` for the backend: `cd backend && uv sync`. Commit `uv.lock`.
4. `frontend/package.json` with `pnpm`: `react@19`, `react-dom@19`, `typescript`, `vite`, `@vitejs/plugin-react`, `@tanstack/react-query`, `yjs`, `y-websocket`, `react-i18next`, `i18next`, `i18next-icu`, `tailwindcss`, `@dnd-kit/core`, `vitest`, `@testing-library/react`, `playwright`, `@axe-core/playwright`, `eslint`, `typescript-eslint`, `eslint-plugin-i18next`, `eslint-plugin-jsx-a11y`.
   Scripts: `dev`, `build`, `lint`, `typecheck`, `test`, `test:e2e`.
5. Root files: `.editorconfig`, `.pre-commit-config.yaml` (ruff, ruff-format, mypy on `backend/`; eslint and prettier on `frontend/`), `Makefile` with targets `setup`, `lint`, `test`, `up`, `down`.
6. Add `README.md` sections "Prerequisites", "Run locally", "Run tests" pointing at the Makefile targets.

**Verify:**
```
cd backend && uv run ruff check . && uv run mypy src && uv run pytest -q
cd frontend && pnpm install && pnpm lint && pnpm typecheck && pnpm test
```
All four must exit 0 on the empty skeleton (a trivial placeholder test in each is fine).

**Commit:** `chore: monorepo skeleton (backend, frontend, profiles, deploy)`

---

## Step 0.2 — CI pipeline

**Why:** The i18n and accessibility gates cannot be retrofitted; they start now.

**Do:**
1. Create `.github/workflows/ci.yml` with four jobs: `backend` (uv sync, ruff, mypy, pytest against PostgreSQL 16 + `pgvector` installed natively on the runner with apt; no service containers or Docker images), `frontend` (pnpm install, lint, typecheck, vitest), `e2e` (build frontend, start a placeholder FastAPI app, Playwright + axe on `/`), `config` (validate every file under `profiles/` against the JSON schemas in Step 0.5).
2. ESLint config must enable `i18next/no-literal-string` as an **error** for `.tsx` files (allow attributes `data-testid`, `className`, `href`, `src`, `type`).
3. Add `frontend/src/i18n/index.ts` initialising `react-i18next` with `i18next-icu`, namespaces `common`, `editor`, `reader`, `graph`, `vocab_admin`, `errors`, `admin`, default `en`, and a `pseudo` locale that wraps every string with `[ ... ]` and expands it ~35% (use `i18next-pseudo` or a 20-line postProcessor).
4. Add a single placeholder route rendering one translated string, and a Playwright test that loads it, runs `axe`, and asserts zero violations.
5. Tailwind config: forbid physical direction utilities by adding an ESLint rule or a `scripts/check-logical-css.sh` grep that fails CI on `ml-`, `mr-`, `pl-`, `pr-`, `left-`, `right-`, `text-left`, `text-right` in `frontend/src`.

**Verify:** Push a branch; all four CI jobs are green. Deliberately add a literal string to a `.tsx` file and confirm the `frontend` job fails, then remove it.

**Commit:** `ci: backend, frontend, e2e (axe), and config validation jobs`

---

## Step 0.3 — Local infrastructure (native)

**Why:** Every spike and every later phase needs Postgres, QLever, and the service processes running the same way.

**Native only.** The project uses no Docker or containers anywhere: not for development, CI, or production (ADR-0017). PostgreSQL 16+ with `pgvector`, and QLever, are installed natively; the `lae` processes run under a Procfile runner in development and systemd in production. On the primary development machine, Postgres.app (PostgreSQL 18 with `vector`, `pg_trgm`, `btree_gin`) and Homebrew `qlever` / `qlever-control` 0.6.0 (`qlever-index`, `qlever-server`, `qlever`) are already installed. `/Users/rs2668/Development/lux/qlever-dev` is the reference source for QLever. Record which QLever binary and version each spike used.

**Do:**
1. `deploy/postgres/init.sql`: `CREATE EXTENSION IF NOT EXISTS` for `vector`, `pg_trgm`, `btree_gin`.
2. `deploy/Qleverfile`: copy the structure of `lux/qlever-middletier/files/Qleverfile`; `SYSTEM = native`; keep `TEXT_INDEX = from_literals` and `ADD_HAS_WORD_TRIPLES = true`; server started with `--persist-updates`; memory sized for a developer laptop; index name `lae`. Port and index directory are overridable by environment variables. QLever binds only to localhost / the internal network; the only public SPARQL entry point is the FastAPI `/sparql` proxy built in Phase 2.
3. `deploy/QLEVER_VERSION`: the QLever binary version verified with (e.g. Homebrew `qlever` 0.6.0_1 and `qlever-server --version` output) plus the `lux/qlever-dev` commit for reference.
4. `deploy/Procfile`: one line per process: `api`, `crdt-ws`, `sync-worker`, `scheduler` (each `uv run --project backend lae <name>`); `embedder`, `reconcile`, `llm` present as commented later-phase entries. Add `honcho` as a backend dev dependency.
5. `deploy/systemd/`: unit templates `lae-api.service`, `lae-crdt-ws.service`, `lae-sync-worker.service`, `lae-scheduler.service`, `qlever.service` (environment from `/etc/lae/lae.env`).
6. `deploy/README.md`: native installation for macOS (Homebrew `qlever` + `qlever-control`; Postgres.app or Homebrew `postgresql` with `pgvector`) and Debian/Ubuntu (PGDG `postgresql-16` + `postgresql-16-pgvector`; QLever from its native packages or built from source), plus the Postgres.app `PATH` line (`/Applications/Postgres.app/Contents/Versions/latest/bin`).
7. `backend/src/lae/cli.py`: a `typer` or `argparse` CLI with sub-commands `api`, `crdt-ws`, `sync-worker`, `scheduler`, `migrate`, `bootstrap` (all placeholders that print their name and exit 0 for now). These names are the Procfile and systemd process names.
8. `backend/src/lae/settings.py`: a `pydantic-settings` `Settings` class with `database_url`, `qlever_url`, `qlever_update_url`, `base_uri` (default `http://localhost:8000/id/`), `context_version` (default `1`), `draft_ttl_days` (5). Load from env with prefix `LAE_`.
9. `Makefile`: `up` creates the database (if missing) and its extensions in an already-running PostgreSQL and starts QLever with `qlever start` from `deploy/`; it never starts or stops PostgreSQL itself. `down` stops QLever. `run` runs `honcho start -f deploy/Procfile`. All ports, the database name, and the index directory are overridable (`PGHOST`, `PGPORT`, `LAE_DB`, `QLEVER_PORT`, `QLEVER_DIR`).

**Verify:** `make up` (with your slot's overrides); `psql` connects and `SELECT extname FROM pg_extension` lists `vector`, `pg_trgm`, `btree_gin`; `curl http://localhost:$QLEVER_PORT` answers; `make run` starts the four placeholder processes and Ctrl-C stops them; `make down` stops QLever.

**Commit:** `infra: native PostgreSQL and QLever setup, Procfile, systemd units`

---

## Step 0.4 — Golden corpus

**Why:** Round-tripping is a hard requirement. The corpus is the oracle for Spike S3 and every later projection change.

**Do:**
1. Write `scripts/sync_corpus.py` that copies every example from the local linked.art documentation checkout, `/Users/rs2668/Development/linked-art/linked.art/docs/example/<category>/*.json` (211 files across `activity`, `concept`, `digital`, `event`, `group`, `object`, `person`, `place`, `provenance`), into `tests/corpus/linkedart/<category>-<basename>.json`, byte-for-byte. Record the source commit hash in `tests/corpus/SOURCE.md`. Also copy the pinned context `linked.art/docs/ns/v1/linked-art.json` to `profiles/context/v1.json`.
2. Add `tests/corpus/institutional/` with at least 20 real records covering `HumanMadeObject`, `Person`, `Group`, `Place`, `Type`, `VisualItem`, `LinguisticObject`, `Set`, `Activity` (ask the domain lead for a sample export; if unavailable, use LUX public records).
3. Write `backend/src/lae/corpus.py` with `iter_corpus() -> Iterator[tuple[Path, dict]]`.
4. Write `backend/tests/test_corpus_loads.py`: every file parses as JSON, has `@context`, `id`, `type`, and the `type` is a known Linked Art class.
5. Vendor the Linked Art JSON Schema from `/Users/rs2668/Development/linked-art/json-validator/schema/*.json` (Draft 7; `core.json` and `linked_art.json` are shared, and `object.json`, `person.json`, `group.json`, `place.json`, `concept.json`, `set.json`, `digital.json`, `text.json`, `image.json`, `event.json`, `provenance.json`, `abstract.json` are the per-class entry points) into `profiles/schema/`, with the source commit in `profiles/schema/SOURCE.md`. Follow `json-validator/schema-test.py` for the `Draft7Validator` usage and resolve `$ref`s between files with a `referencing` registry. Add `backend/tests/test_corpus_schema.py` asserting every corpus file validates against the schema for its `type` (document any that do not in `tests/corpus/KNOWN_FAILURES.md` rather than deleting them).

**Verify:** `uv run pytest tests/test_corpus_loads.py tests/test_corpus_schema.py -q` passes.

**Commit:** `test: golden Linked Art corpus and schema validation`

---

## Step 0.5 — Core database schema (migration 0001)

**Why:** Phase 1 builds on these tables. Get the shape right once.

**Do:**
1. `backend/src/lae/db/models.py` using SQLAlchemy 2 declarative with these tables (column types in parentheses):
   - `records`: `id (UUID pk, UUIDv7)`, `uri (text unique)`, `type (text, indexed)`, `version (int)`, `status (enum: active, deleted, merged)`, `etag (text)`, `data (JSONB)`, `context_version (int)`, `created_at`, `updated_at`, `merged_into (UUID null)`.
     After `create_table`, run `ALTER TABLE records ALTER COLUMN data SET COMPRESSION lz4`.
   - `record_revisions`: `id (bigint pk)`, `record_id (UUID fk)`, `version (int)`, `ntriples_zstd (bytea)`, `jsonld_zstd (bytea)`, `context_version (int)`, `authors (UUID[])`, `published_by (UUID)`, `summary (text)`, `accepted_warnings (JSONB)`, `merge_batch_id (UUID null)`, `split_batch_id (UUID null)`, `created_at`. Unique on `(record_id, version)`.
   - `entity_labels`: `uri (text pk)`, `type (text)`, `labels (JSONB)`, `default_label (text not null)`, `primary_classification (text null)`, `metatype_uris (text[])`, `vocab_ids (text[])`, `source (text)` (e.g. `local`, `aat`), `status (enum: published, draft)`.
   - `crdt_drafts`: `record_id (UUID pk)`, `draft_epoch (int)`, `base_version (int)`, `state (bytea)`, `contributors (UUID[])`, `last_activity_at`, `created_by`.
   - `crdt_updates`: `id (bigint pk)`, `record_id (UUID fk)`, `draft_epoch (int)`, `update (bytea)`, `created_at`.
   - `outbox_events`: `id (bigint pk)`, `kind (enum: publish, delete, merge, split)`, `record_id (UUID)`, `from_version (int null)`, `to_version (int null)`, `payload (JSONB)`, `status (enum: pending, done, failed)`, `attempts (int)`, `last_error (text)`, `created_at`, `processed_at`.
   - `sync_state`: `key (text pk)`, `value (JSONB)` — holds `qlever_index_watermark`.
   - `users`: `id (UUID pk)`, `subject (text unique, OIDC sub)`, `display_name`, `email`, `kind (enum: human, agent)`, `is_active`.
   - `roles`, `user_roles`, `api_tokens` (`token_hash`, `scopes (text[])`, `expires_at`, `last_used_at`).
2. Alembic: `backend/alembic.ini`, `backend/migrations/env.py` (async engine), `0001_core.py` generated from the models and then hand-checked.
3. Add a trigger in the migration: `AFTER INSERT ON outbox_events` → `pg_notify('outbox', NEW.id::text)`.
4. `lae migrate` runs `alembic upgrade head`.
5. `backend/tests/conftest.py`: a session fixture that reads `LAE_TEST_DATABASE_URL` (a native PostgreSQL; required), drops and recreates that database, applies `deploy/postgres/init.sql`, runs migrations, and yields an async session; the database is dropped at the end of the session.

**Verify:** `uv run lae migrate` on a fresh database succeeds; `uv run alembic downgrade base && uv run alembic upgrade head` round-trips; `uv run pytest -q` passes with one test inserting and reading a `records` row.

**Commit:** `db: core schema migration 0001 (records, revisions, labels, crdt, outbox, auth)`

---

## Step 0.6 — Spike S1: QLever updates, persistence, text freshness, rebuild timing

**Why:** The sync design assumes SPARQL Update works on this fork, survives restarts, and that text search sees new literals. None of that is proven.

**Do:**
1. Create `scripts/spikes/s1_qlever/`. Generate 10M synthetic triples (`scripts/spikes/s1_qlever/gen.py`: 100k subjects × ~100 triples each, including `rdfs:label` literals) into `s1.nt`.
2. Build the index natively with `qlever index` (qlever-control, `deploy/Qleverfile`) or `qlever-index` directly, as in `lux/qlever-middletier/installing_qlever.md`. Record wall-clock and peak RSS.
3. Start the server with `--persist-updates`. Run and time:
   - `INSERT DATA { <s> rdfs:label "zebra-crossing-xyz" }` with 100 triples.
   - `DELETE DATA` of the same.
   - `DELETE WHERE { <s> ?p ?o }` for one subject.
   - 1,000 sequential small updates; record p50/p95 latency and the `qlever_delta_triples` metric.
4. Text freshness: after inserting a literal, run `?s ql:contains-word "zebra*"` (prefix) and `?s ql:has-word "zebra-crossing-xyz"`. Record whether each finds the new literal **without a rebuild**. The source in `qlever-dev/src/index/DeltaTriples.cpp` adds `ql:has-word` triples on update when the index was built with `ADD_HAS_WORD_TRIPLES = true`, so `has-word` is expected to work; the prefix case is the real question.
5. Restart the QLever server process. Confirm the inserted triples are still present (persistence) and record restart time.
6. SERVICE: run `scripts/spikes/s1_qlever/stub_service.py` (FastAPI, copy the request parser from `lux/qlever-middletier/sparql_service/sparql.py`) and issue a QLever query with `SERVICE <http://stub:8081/sparql> { ?x <urn:vec:score> ?score }` plus a `VALUES` sibling of 5,000 IRIs. Record whether VALUES were pushed down and the `service-max-value-rows` behaviour.
7. Rebuild timing: rebuild the index from scratch while the old server keeps answering queries from a second directory; record the time. Extrapolate to 1B triples.
8. Write `docs/spikes/S1-qlever.md` with a results table and a **GO / NO-GO** line per question, plus the chosen fallback where NO-GO.

**Verify:** The report exists and every question in step 3–7 has a measured answer, not a guess.

**Commit:** `spike(S1): QLever update/persistence/text/SERVICE/rebuild results`

---

## Step 0.7 — Spike S2: pycrdt-websocket under load

**Do:**
1. `scripts/spikes/s2_crdt/server.py`: minimal FastAPI app mounting `pycrdt_websocket.ASGIServer` with an in-memory room store, one room per path.
2. `scripts/spikes/s2_crdt/client_load.py`: using `pycrdt` + `websockets`, simulate 30 clients across 10 rooms, each applying one Y.Map set and one Y.Text insert per second for 5 minutes, with awareness updates every 2 s. Five clients disconnect and reconnect every 30 s.
3. Measure: server RSS per room, update propagation p95 latency (timestamp inside the update payload), any dropped or divergent state (compare final `Doc.get_state()` across clients), and behaviour on reconnect.
4. Also connect one real browser client (`y-websocket` provider in a 30-line Vite page) to confirm wire compatibility.
5. Write `docs/spikes/S2-pycrdt-websocket.md` with GO / NO-GO. If NO-GO, name the fallback (Hocuspocus sidecar) and the interface boundary (`lae.crdt.rooms.RoomManager`).

**Commit:** `spike(S2): pycrdt-websocket load results`

---

## Step 0.8 — Spike S3: JSON-LD ↔ Y.Doc projection round-trip

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: the projection rules define lossless round-tripping and concurrent merge semantics; a wrong rule silently corrupts records for every later phase.


**Why:** This is the single most important correctness property in the system.

**Do:**
1. Implement a **throwaway** prototype in `scripts/spikes/s3_projection/` (it will be rewritten properly in Phase 1, but the rules must be proven now):
   - `hydrate(jsonld: dict) -> pycrdt.Doc` using these rules:
     - Root `Doc` has one `Map` named `record`. Each top-level JSON key becomes a key in it.
     - A JSON array whose items are objects with `type` becomes a `Map` keyed by node id. Embedded nodes without an `id` get an internal node IRI `<record_uri>/node/<uuidv7>` stored under key `id`. Items that are references (have `id` and no other keys except `type`, `_label`) are keyed by their `id`.
     - Every entry of such a `Map` gets a float `_order` = its original array index (0.0, 1.0, …). New inserts take midpoint values.
     - Arrays of scalars (e.g. `equivalent` may be objects; plain string arrays like `"notation"`) become a `Map` keyed by the string value with `_order`.
     - `content` strings on `Name`, `Identifier`, `LinguisticObject` become `Text`. All other scalars are plain values.
     - Nested objects without `type` (rare) are stored as plain JSON values.
   - `project(doc) -> dict` reversing the above: `Map`s back to arrays sorted by `_order` then key; `_order` removed; internal node IRIs **kept** (stripping is a separate `strip_internal_iris()` step).
   - `canonical_json(d) -> str`: `orjson` with sorted keys.
2. Test: for every corpus file, `canonical_json(strip_internal_iris(project(hydrate(x)))) == canonical_json(x)`. Array order must be preserved exactly.
3. Property test with `hypothesis`: generate random Linked-Art-shaped documents (nested names, identifiers, references), round-trip, compare.
4. Concurrency test: two `Doc`s from the same hydration; client A inserts a `Name` at position 1, client B inserts a `Name` at position 1; exchange updates; assert both project to the same array and that neither insert was lost.
5. Write `docs/spikes/S3-projection.md` with the final rule list (this becomes the basis of the Phase 1 projection spec) and GO / NO-GO.

**Commit:** `spike(S3): projection round-trip rules and results`

---

## Step 0.9 — Spike S4: SHACL latency

**Do:**
1. Write `profiles/shapes/HumanMadeObject.ttl` with 10 realistic constraints (class of `produced_by`, `TimeSpan` ordering, `made_of` target class, required `identified_by` Name, etc.).
2. `scripts/spikes/s4_shacl/bench.py`: expand each corpus `HumanMadeObject` to a graph with `pyld` + the pinned context, run `pyshacl.validate`, record per-record ms (warm, 50 iterations).
3. Try `pyshacl` with `advanced=False`, with pre-loaded shapes graph, and with `pyoxigraph` as the data store if supported. Record each.
4. Write `docs/spikes/S4-shacl.md`: table of timings, and a recommended per-record budget. GO if p95 < 100 ms; otherwise list which constraints move to the Python rule engine.

**Commit:** `spike(S4): SHACL latency results`

---

## Step 0.10 — Spike S5: Canvas Panel in React 19

**Do:**
1. In `frontend/`, add `@digirati/canvas-panel-web-components` (confirm the current package name on npm) and a page `src/spikes/CanvasPanelSpike.tsx` rendering `<canvas-panel>` for a public IIIF manifest.
2. Prove: load by manifest + canvas id; set `region` programmatically; listen to a selection event and read back a region as `xywh`; load an `iiif-content` Content State; render `<sequence-panel>` for navigation; load an IIIF A/V manifest in Canvas Panel and record whether it plays. Canvas Panel is the only viewer (ADR-0012): no Clover or Mirador. If a capability is missing, record the gap and add a `docs/questions.md` entry.
3. Record bundle size impact and any React 19 custom-element prop issues.
4. Write `docs/spikes/S5-canvas-panel.md` with GO / NO-GO.

**Commit:** `spike(S5): Canvas Panel results`

---

## Step 0.11 — Spike S6: bulk bootstrap timing

**Do:**
1. `scripts/spikes/s6_bootstrap/`: generate 1M synthetic Linked Art records (vary the corpus templates), write them with `COPY` into `records`, expand each to N-Triples with `pyld` using the pinned context (multiprocessing), write one `.nt` file, build the QLever index.
2. Record: COPY rate (records/s), expansion rate, N-Triples size, index build time and RSS.
3. Write `docs/spikes/S6-bootstrap.md` with the extrapolation to 10M and the per-stage bottleneck.

**Commit:** `spike(S6): bulk bootstrap timing results`

---

## Step 0.12 — Config schemas for `profiles/`

**Why:** UI Profiles and Value Sets are data; CI must validate them from the start.

**Do:**
1. `profiles/ui/_schema.json`: JSON Schema for a UI Profile: `class`, `version`, `sections[]` (`id`, `label: LangMap`, `slots[]`), `slot` (`id`, `label: LangMap`, `help_text: LangMap?`, `path` (JSON-path-like string), `widget` (enum: `text`, `langtext`, `edtf`, `entity_ref`, `classification`, `dimension`, `identifier`, `statement`, `wkt`, `production`), `value_set?`, `multiple: bool`, `required: bool`, `advanced: bool`).
2. `profiles/vocab/_schema.json`: Value Set definition (`id`, `label: LangMap`, `mode: enumerated|rule`, `terms[]` or `rule {metatype_uris[], broader_roots[], exclusions[], classes[]}`, `enforcement: strict|extensible|advisory`), and slot bindings (`class`, `path`, `value_set`).
3. `LangMap` = object with BCP-47 keys and string values, `en` required.
4. `scripts/validate_profiles.py` validating every YAML in `profiles/ui` and `profiles/vocab` against these schemas; wired into the CI `config` job.
5. Write one real profile, `profiles/ui/HumanMadeObject.yaml`, with the core slots (title, identifiers, type of work, creator, production date, production place, materials, dimensions, description) — content only; no renderer yet.

**Verify:** `python scripts/validate_profiles.py` exits 0; breaking a label into a bare string makes it fail.

**Commit:** `config: UI Profile and Value Set schemas with CI validation`

---

## Exit Checklist

- [ ] `make setup && make lint && make test` pass on a clean clone.
- [ ] CI green on `main` with all four jobs.
- [ ] `make up` brings up Postgres and QLever; `lae migrate` applies migration 0001.
- [ ] `tests/corpus/` contains all linked.art examples plus ≥20 institutional records, and both corpus tests pass.
- [ ] `docs/spikes/S1..S6` exist, each with measured results and a GO / NO-GO line.
- [ ] Any NO-GO has been reported to a human with the named fallback. Do not start Phase 1 until a human has acknowledged every NO-GO.
- [ ] `profiles/ui/HumanMadeObject.yaml` validates.

## Common Mistakes

- Building the index without `--persist-updates`, then concluding updates are lost on restart.
- Measuring QLever text search right after a rebuild and concluding freshness is fine. The test must insert **after** the build.
- Letting the S3 prototype drift into the real implementation. It is a spike; Phase 1 rewrites it with tests.
- Forgetting `_order` on reference lists, not just node lists. Both must round-trip in order.
- Adding a UI component in this phase. Nothing in `frontend/src` beyond the i18n scaffold, the placeholder route, and the Canvas Panel spike belongs here.
