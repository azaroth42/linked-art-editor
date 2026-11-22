# Phase 1 — Core Authoring Loop: Step-by-Step Guide

**Read first:** `docs/README.md`, `docs/conventions.md`, `docs/glossary.md`, `docs/spec/` (section numbers are preserved from the original design document) sections 3, 4, 5, 6.1, 6.2, 6.3, 8, 12, the Phase 0 spike reports in `docs/spikes/`, and `docs/reuse_map.md` (sections for `qlever-middletier`, `data-pipeline`, `linkedart-parser`).
**Goal:** Catalogers co-edit and publish real `HumanMadeObject`, `Person`, `Place`, and `Type` records. Every publish is queryable in QLever within a second. The golden corpus round-trips through the full stack.
**You are done when** every item in the Exit Checklist passes.

Rules for this phase:
- Phase 0's Exit Checklist must be fully ticked, including human acknowledgement of any NO-GO.
- Backend before frontend within each feature: the API and its tests exist before the UI that calls it.
- The projection rules from `docs/spikes/S3-projection.md` are the specification for Step 1.4. Do not invent new ones; if one is missing, add it to the spike report and `docs/questions.md`.
- Nothing from later phases: no Value Set registry, no merge, no agents, no explorers. A hand-written static Value Set bundle is allowed (Step 1.17).

---

## Step 1.1 — Application factory and process entrypoints

**Why:** Four processes share one package. They must start the same way and log the same way.

**Do:**
1. `lae/app.py`: `create_app(settings) -> FastAPI` registering routers from `lae.api`, a lifespan that opens the SQLAlchemy async engine (`lae/db/session.py`, `get_session` dependency) and an `httpx.AsyncClient`, and `GET /health/live` (always 200) and `GET /health/ready` (checks Postgres `SELECT 1` and, when `LAE_QLEVER_URL` is set, a trivial QLever query).
2. `lae/logging.py`: structured JSON logs via `structlog` with `request_id`, `user_id`, `record_id` bound where known.
3. `lae/cli.py` sub-commands now do real work: `api` runs uvicorn on `create_app`; `crdt-ws` runs the room server (Step 1.8); `sync-worker` runs `lae.sync.worker.main`; `scheduler` runs `lae.scheduler.main` (Step 1.15). Each reads `Settings`.
4. `lae/settings.py` gains: `internal_token`, `qlever_access_token`, `crdt_ws_url` (how `api` reaches `crdt-ws`, default `http://crdt-ws:8001`), `oidc_issuer`, `oidc_client_id`, `oidc_client_secret`, `session_secret`, `dev_user` (optional; see Step 1.7).

**Verify:** `uv run lae api` serves `/health/live` → `{"status":"ok"}`; `/health/ready` → 200 with Postgres and QLever up, 503 with Postgres down. `uv run pytest tests/test_health.py`.

**Commit:** `feat(app): application factory, health endpoints, process entrypoints`

---

## Step 1.2 — Identifiers and URIs

**Do:**
1. `lae/ids.py`: `new_uuid7() -> UUID` (from `uuid_utils`), `record_uri(record_id) -> str` (`settings.base_uri + str(id)`), `record_id_from_uri(uri) -> UUID | None`, `node_iri(record_uri) -> str` (`f"{record_uri}/node/{new_uuid7()}"`), `is_internal_node_iri(iri) -> bool`, `owner_of(iri) -> str` (the record URI for a node IRI).
2. Tests for every function including non-local URIs returning `None`.

**Verify:** `uv run pytest tests/test_ids.py`.

**Commit:** `feat(ids): UUIDv7 record and internal node IRIs`

---

## Step 1.3 — RDF layer: expansion, canonical N-Triples, diff

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: an ownership or expansion error emits or deletes triples belonging to other records in QLever, and the damage is invisible until a rebuild.


**Why:** The sync worker and revisions depend on exact, reproducible triples.

**Do:**
1. `lae/rdf/context.py`: `load_context(version: int) -> dict` reading `profiles/context/v{version}.json`; a pyld document loader that serves **only** these files and raises for any other URL (no network, ever).
2. `lae/rdf/expand.py`:
   - `strip_stub_attributes(jsonld, record_uri) -> dict`: for every reference to a URI not owned by this record (not the record URI and not one of its node IRIs), keep only `id` and `type` … then **drop `type` too** before expansion, because `rdf:type` of a foreign subject must not be emitted (spec §3.3). Implement by replacing foreign references with `{"id": uri}` only.
   - `expand_to_ntriples(jsonld, context_version) -> frozenset[str]`: `pyld.jsonld.to_rdf(doc, {"format": "application/n-quads"})` with the pinned context, drop graph components, one triple per line in N-Triples syntax, deduplicated. Blank nodes must not occur; raise `BlankNodeError` if any line contains `_:` (it means a node lacked an internal IRI).
   - Apply `clean_iri` / `clean_datetime` sanitation vendored from `lux/data-pipeline/lux_pipeline/sources/lux/qlever/mapper2.py`; reject rather than emit a bad literal.
   - `owned_subjects(jsonld, record_uri) -> set[str]`.
3. `lae/rdf/canonical.py`: `canonical_ntriples(triples) -> bytes` (sorted lines, `\n` separated, UTF-8); `compress(b) -> bytes` / `decompress(b) -> bytes` with `zstandard` level 9; `parse_ntriples(b) -> frozenset[str]`.
4. `lae/rdf/diff.py`: `diff_triples(old: frozenset[str], new: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]` returning `(to_delete, to_insert)`.
5. `lae/sparql/terms.py`: vendor `terms.py` (`iri`, `string_literal`) and its tests from `lux/qlever-middletier/qleverlux/sparql/` (Phase 2 adds `builder.py` beside it). `lae/sparql/updates.py`: `build_update(delete: Iterable[str], insert: Iterable[str]) -> str` producing `DELETE DATA { ... } ; INSERT DATA { ... }` (empty parts omitted), and `build_delete_where_subjects(subjects: Iterable[str]) -> str` producing one `DELETE WHERE { <s> ?p ?o }` per subject joined by `;`.
6. `lae/rdf/serialize.py`: `to_turtle(jsonld, context_version) -> str` via pyoxigraph for the public Turtle representation (after `strip_internal_iris`, blank nodes are acceptable here).
7. Tests: every corpus file (with internal IRIs assigned by Step 1.4) expands without blank nodes; expansion is deterministic across two runs; `diff_triples` of a record with one changed title yields exactly one delete and one insert; `build_update` output parses with pyoxigraph's SPARQL parser.

**Verify:** `uv run pytest tests/rdf -q`.

**Commit:** `feat(rdf): pinned-context expansion, canonical zstd N-Triples, triple diff, SPARQL update builder`

---

## Step 1.4 — Projection layer (JSON-LD ↔ Y.Doc)

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: this is the production implementation of the round-trip and `_order` rules; bugs here lose cataloger data without any error.


**Why:** The spike proved the rules; this is the production implementation with tests.

**Do:**
1. `lae/projection/rules.py`: constants `TEXT_CONTENT_TYPES = {"Name", "Identifier", "LinguisticObject"}`, `ROOT_MAP = "record"`, `ORDER_KEY = "_order"`, and `is_reference(obj) -> bool` (has `id`, and no keys beyond `id`, `type`, `_label`).
2. `lae/projection/hydrate.py`: `assign_node_iris(jsonld, record_uri) -> dict` (adds `id` to every embedded node lacking one, recursively, never to references) and `hydrate(jsonld, record_uri) -> pycrdt.Doc` implementing the S3 rules exactly (keyed `Map`s for node arrays and reference arrays with `_order`, `Text` for `content` on the three content types, scalars as values, unknown keys preserved verbatim).
3. `lae/projection/project.py`: `project(doc) -> dict` (sort by `_order` then key, drop `_order`, `Text` back to string) and `strip_internal_iris(jsonld) -> dict` (remove `id` whose value `is_internal_node_iri`; keep references untouched).
4. `lae/projection/ops.py`: helpers used by the API and later by MCP tools: `get_path(doc, path)`, `set_scalar(doc, path, value)`, `add_node(doc, path, node: dict) -> str` (mints a node IRI, assigns `_order` after the last entry), `add_reference(doc, path, uri, type)`, `remove_entry(doc, path, key)`, `move_entry(doc, path, key, before_key | None)` (fractional index between neighbours). Paths use the UI Profile path syntax (Step 0.12), e.g. `identified_by[Name].content`.
5. `lae/projection/canonical_json.py`: `canonical_json(d) -> bytes` with `orjson.OPT_SORT_KEYS`.
6. Tests: `tests/test_roundtrip.py` over the whole corpus (`canonical_json(strip_internal_iris(project(hydrate(x)))) == canonical_json(x)`), the hypothesis property test from S3 moved here, the concurrent-insert convergence test, and tests for every `ops` function.

**Verify:** `uv run pytest tests/projection tests/test_roundtrip.py -q`. Round-trip must pass for 100% of the corpus (known failures are not allowed here; fix the rules).

**Commit:** `feat(projection): hydrate/project with _order, internal IRIs, ops helpers, corpus round-trip`

---

## Step 1.5 — Stub normalization and label transclusion

**Do:**
1. `lae/labels/extract.py`: `labels_from_record(jsonld) -> tuple[dict[str, str], str]` returning `(labels_by_lang, default_label)`. Rules: take `identified_by` entries of type `Name`; a Name classified as Primary Name (`aat:300404670`) wins; language from `language[0].notation` or the AAT language's known tag (map the ~40 common AAT language URIs to BCP-47 in `lae/labels/langtags.py`); if no language, treat as `und`; `default_label` = English primary name, else any primary name, else first Name, else `_label`, else the record id.
2. `lae/labels/normalize.py`: `normalize_refs(jsonld, known: set[str]) -> dict`: for each reference whose `id` is in `known`, drop `_label`; otherwise keep it. `known` comes from one `SELECT uri FROM entity_labels WHERE uri = ANY($1)` over all reference ids in the document.
3. `lae/labels/transclude.py`: `hydrate_labels(jsonld, locale_chain: list[str], session) -> dict`: collect all reference ids, one query to `entity_labels`, set `_label` from the first locale in the chain present in `labels`, else `default_label`; references not found keep whatever `_label` they have.
4. `lae/labels/upsert.py`: `upsert_entity_labels_for_record(session, record)` writing `uri`, `type`, `labels`, `default_label`, `primary_classification` (first `classified_as` id), `source='local'`, `status='published'`.
5. `lae/api/deps.py`: `locale_chain(request) -> list[str]` parsing `Accept-Language` with RFC 4647 basic filtering and appending `en`.
6. Tests with corpus records in several languages.

**Verify:** `uv run pytest tests/labels -q`.

**Commit:** `feat(labels): label extraction, stub normalization, locale-aware transclusion`

---

## Step 1.6 — Records API and public dereference

**Do:**
1. `lae/api/records.py` (prefix `/api/records`):
   - `POST /api/records` body `{type: str, template?: dict}` → creates a `records` row with `status='active'`, `version=0`, `data` = minimal skeleton (`@context`, `id`, `type`), opens a draft (Step 1.8) and returns `{id, uri, draft_epoch}`. Requires `edit_draft`.
   - `GET /api/records/{id}` → published JSON-LD, hydrated with labels, internal IRIs **kept** (the editor needs them). Requires authentication. 404 if `version=0` and no draft.
   - `GET /api/records/{id}/revisions` and `GET /api/records/{id}/revisions/{n}` (Step 1.14 fills these in).
2. `lae/api/public.py`:
   - `GET /id/{uuid}`: content negotiation on `Accept`. `application/ld+json` (with or without the Linked Art profile parameter) → stripped, hydrated JSON-LD with `Content-Type: application/ld+json;profile="https://linked.art/ns/v1/linked-art.json"`; `text/turtle` → Turtle; `text/html` or default → the reader template (Step 1.21; until then a minimal HTML page with the label and links to the two serializations). Query `?version=N` serves that revision's stored JSON-LD. `status='deleted'` → 410; `status='merged'` → 301 to `merged_into`. Never requires auth. `ETag` and `Last-Modified` headers set; `If-None-Match` honoured.
   - `GET /id/{uuid}/node/{nid}` → 303 to `/id/{uuid}`.
3. Tests for each media type, each status, versioned fetch, and that no internal IRI appears in any public body.

**Verify:** `uv run pytest tests/api/test_records.py tests/api/test_public.py -q`.

**Commit:** `feat(api): records create/read and public /id/ content negotiation`

---

## Step 1.7 — Authentication and permissions

**Do:**
1. `lae/auth/oidc.py` with Authlib: `GET /auth/login` (redirect to the issuer), `GET /auth/callback` (exchange code, upsert `users` by `subject`, set a signed session cookie), `POST /auth/logout`, `GET /auth/me` → `{id, display_name, roles, permissions}`.
2. `lae/auth/tokens.py`: Personal Access Tokens. `POST /api/tokens` (name, scopes, expires_at) returns the plaintext once; stored as SHA-256 in `api_tokens.token_hash`. Bearer auth resolves to the user; `last_used_at` updated at most once a minute.
3. `lae/auth/permissions.py`: `ROLE_PERMISSIONS` mapping `Viewer` → `{}`; `Collaborator` → `{edit_draft}`; `Editor` → `{edit_draft, publish, delete, merge, unmerge, protect}`; `System Admin` → everything. `current_user` dependency and `require(*perms)` dependency raising 403 with diagnostic `LAE-AUTH-001`.
4. Dev mode: if `LAE_DEV_USER` is set (e.g. `editor@example.org:Editor`), every request runs as that user with that role. Refused if `LAE_ENV=production`.
5. Migration `0002_auth_seed.py`: insert the four roles.
6. Wire `require("edit_draft")` and `require("publish")` into the existing endpoints. Tests: 401 anonymous on `/api/records`, 403 Viewer on `POST /api/records`, PAT round-trip.

**Verify:** `uv run pytest tests/auth -q`; manual login against a local Keycloak or the institution's test IdP documented in `docs/dev-setup.md`.

**Commit:** `feat(auth): OIDC login, PATs, role permissions, dev user`

---

## Step 1.8 — CRDT room manager and WebSocket server (`crdt-ws`)

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: persistence ordering, the epoch guard, compaction, and rehydration decide whether edits survive crashes and publishes.


**Why:** The single live draft per record lives here. Everything that edits goes through it.

**Do:**
1. `lae/crdt/rooms.py`: `class RoomManager(Protocol)` with `async open(record_id) -> Room`, `async get(record_id) -> Room | None`, `async is_open(record_id) -> bool`, `async snapshot(record_id) -> Snapshot` (`jsonld` projected with internal IRIs, `contributors`, `draft_epoch`, `base_version`), `async close(record_id, *, discard: bool)`, `async apply_update(record_id, update: bytes, user_id)`, `async list_rooms()`, `async eject(user_id)` (disconnect every socket of a user; used by admin suspension in Phase 4). Phase 3 adds `transact(record_id, actor, fn) -> str` (one tagged Yjs transaction, returns its id) and `revert(record_id, transaction_ids)`; declare them in the Protocol now with `NotImplementedError` bodies so later phases extend rather than reshape. `class InMemoryRoomManager` implementing it with `pycrdt`.
2. Hydration on `open`: if a `crdt_drafts` row exists, load `state` and replay any `crdt_updates` with the same `draft_epoch` after compaction; else `hydrate(records.data)` and insert a `crdt_drafts` row with `draft_epoch = records.draft_epoch`, `base_version = records.version`. Migration `0003_records_draft_epoch.py` adds `records.draft_epoch int default 0`.
3. Persistence: every incoming update is appended to `crdt_updates` **before** it is broadcast. Every 200 updates, or on idle eviction, compact: write `Doc.get_update()` to `crdt_drafts.state` and delete the compacted `crdt_updates` rows. `last_activity_at` updated on every write; `contributors` gains the user id.
4. `lae/crdt/server.py`: an ASGI app for the `crdt-ws` process. Route `/ws/rooms/{record_id}` using `pycrdt_websocket.ASGIServer` with a custom room store backed by `InMemoryRoomManager`. On connect: authenticate via session cookie or `?token=` PAT; require `edit_draft` (Viewers get read-only: their updates are dropped and they are told via an awareness flag); compare the client's `X-Draft-Epoch` header (or first message) with the room's epoch and close with code `4409` and reason `stale-epoch` if older. Awareness payload includes `{user_id, display_name, kind: "human"|"agent"}`.
5. Internal RPC (same ASGI app, on the internal network, header `X-Internal-Token`): `GET /internal/rooms`, `GET /internal/rooms/{id}/snapshot`, `POST /internal/rooms/{id}/close` body `{discard: bool}`, `POST /internal/rooms/{id}/apply` body `{update: base64, user_id}` (used by MCP in Phase 3).
6. `lae/crdt/client.py`: `CrdtWsClient` used by `api` to call the internal RPC.
7. Idle eviction: rooms with no connections and no writes for 10 minutes are compacted and dropped from memory (the draft row persists).
8. Tests: open → write → snapshot reflects write; restart the manager → room rehydrates from `crdt_drafts` + `crdt_updates`; stale epoch connection is rejected; two clients converge.

**Verify:** `uv run pytest tests/crdt -q`; run `uv run lae crdt-ws` and connect the S2 browser page to `/ws/rooms/<id>`.

**Commit:** `feat(crdt): RoomManager, persistent rooms, websocket server with epoch guard and internal RPC`

---

## Step 1.9 — Validation engine

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: mapping SHACL results back to JSON paths and keeping schema, rules, and shapes consistent is subtle; wrong mappings block or mis-report publishes.


**Do:**
1. `lae/validation/diagnostics.py`: `Diagnostic(code: str, path: str, severity: Literal["violation","warning","info"], params: dict, message_en: str)` and `Diagnostics(items, ok: bool)` where `ok` is "no violations".
2. `lae/validation/schema.py`: load `profiles/schema/` once with a `referencing` registry; `schema_for_type(type) -> Validator`; `validate_schema(jsonld) -> list[Diagnostic]` mapping each `jsonschema` error to code `LAE-SCHEMA-<validator>` (e.g. `LAE-SCHEMA-required`), `path` as a JSON Pointer, `params` = `{validator_value, instance_summary}`. Before validating, run `strip_internal_iris` (the public schema does not allow `id` on embedded nodes).
3. `lae/validation/shacl.py`: load all `profiles/shapes/*.ttl` into one shapes graph at startup; `validate_shacl(jsonld, record_uri, referenced_types: dict[str,str]) -> list[Diagnostic]`: build the data graph from `expand_to_ntriples` (with internal IRIs so paths resolve) plus one `rdf:type` triple per referenced entity from `entity_labels.type`; run `pyshacl.validate` in a thread; map each result's `sh:resultSeverity` to severity, `sh:resultPath`/focus node back to a JSON path via a node-IRI → path index built during expansion, and read `code` from a custom `lae:code` annotation on the shape (every shape must carry one). Respect the latency budget from `docs/spikes/S4-shacl.md`: if a document exceeds it, log and continue.
4. `lae/validation/rules.py`: Python rules for things SHACL handles poorly: `TimeSpan` `begin_of_the_begin <= end_of_the_end` (`LAE-RULE-001`), EDTF syntax in `identified_by` of a `TimeSpan` (`LAE-RULE-002`), empty `content` (`LAE-RULE-003`), reference to a `status='deleted'` record (`LAE-RULE-004`).
5. `validate(jsonld, record_uri, session) -> Diagnostics` running schema → (if no schema violations) rules + SHACL.
6. Write the Phase 1 shapes: `profiles/shapes/common.ttl` (Name/Identifier/TimeSpan), `HumanMadeObject.ttl` (from S4), `Person.ttl`, `Place.ttl`, `Type.ttl`, each constraint with `lae:code`. Keep them small; Phase 2 compiles Value Sets into them.
7. Endpoints: `POST /api/records/{id}/draft/validate` (snapshot from `crdt-ws`, then `validate`) and `POST /api/validate` body `{jsonld}` for CLI and batch use.
8. Add every diagnostic code with an English message to `frontend/public/locales/en/errors.json` and a test that every code emitted in tests has an entry.

**Verify:** `uv run pytest tests/validation -q`; the corpus produces zero violations (warnings allowed; list them in `docs/spikes/S4-shacl.md` appendix).

**Commit:** `feat(validation): JSON-Schema, SHACL with lae:code, Python rules, structured diagnostics`

---

## Step 1.10 — Publish transaction

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: the single-transaction publish with bundled stubs, ordered locking, and the drift flag is the system's integrity boundary.


**Why:** This is the one place where drafts become truth. Everything is in a single database transaction.

**Do:**
1. `lae/publish/service.py`: `async publish(record_id, user, summary, accepted_warnings: list[str]) -> PublishResult`:
   1. Require `publish`. Fetch the snapshot from `crdt-ws`. If no room and no draft row → 409 `LAE-PUB-001`.
   2. Collect **dependent stubs**: referenced ids that are local record URIs whose `records.version = 0` (never published). For each, fetch its snapshot too. Validate each document; any violation anywhere aborts with the diagnostics grouped by record id. Every warning code+path must appear in `accepted_warnings` or the call fails with `LAE-PUB-002`.
   3. Open one transaction. For every document (primary + stubs), locked in UUID order with `SELECT ... FOR UPDATE`:
      - `normalize_refs`; set `version = version + 1`, `etag`, `data`, `context_version`, `draft_epoch = draft_epoch + 1`, `updated_at`.
      - Compute `new_triples = expand_to_ntriples(data)`. `old_triples` = the previous revision's decompressed N-Triples if a revision exists, else (bulk-loaded, version was 1 without a revision) `expand_to_ntriples(previous data)` and set `payload.drift_check = True`.
      - Insert `record_revisions` (`ntriples_zstd`, `jsonld_zstd` of `data`, `authors` = draft contributors ∪ {user}, `published_by`, `summary`, `accepted_warnings`).
      - `upsert_entity_labels_for_record`.
      - Insert `outbox_events` (`kind='publish'`, `from_version`, `to_version`, `payload = {context_version, drift_check}`).
      - Delete `crdt_drafts` and `crdt_updates` for the record.
   4. Commit. Then call `crdt-ws` `close(discard=True)` for each room (best effort; the epoch bump already invalidates stale clients).
2. Endpoint `POST /api/records/{id}/publish` body `{summary: str, accepted_warnings: [{code, path}]}` → `{version, published: [ids]}`.
3. `POST /api/records/{id}/draft/discard` (requires `publish`): closes the room with `discard=True`, deletes draft rows, and if `version = 0` deletes the record row. All draft-scoped endpoints in every phase use the `/api/records/{id}/draft/...` prefix; there is no separate `/api/drafts/{id}/...` family except the listing `GET /api/drafts`.
4. Tests: publish a corpus record end to end (counts of rows in each table); dependent stub bundled; violation blocks and nothing is written; unaccepted warning blocks; concurrent double-publish yields exactly one new version.

**Verify:** `uv run pytest tests/publish -q`.

**Commit:** `feat(publish): atomic publish with bundled stubs, revisions, labels, outbox, draft clear`

---

## Step 1.11 — Sync worker (Postgres → QLever)

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: the diff and drift logic, idempotent replay, and watermark semantics determine whether QLever ever diverges from Postgres.


**Do:**
1. `lae/sync/qlever_client.py`: `QLeverClient(base_url, update_url, access_token)` following the pattern in `lux/qlever-middletier/qleverlux/clients/qlever.py` **without** the result cache: `async query(sparql) -> list[dict]` (`Accept: application/qlever-results+json`, 30 s timeout) and `async update(sparql) -> None` (POST to the update endpoint with the access token; raise on non-2xx with the body in the exception).
2. `lae/sync/worker.py` `main()`:
   - Connect with `asyncpg`, `LISTEN outbox`; also poll every 1 s (notifications can be missed).
   - Batch: take up to 50 `pending` events ordered by id, or whatever has arrived within 250 ms.
   - For each event: load `to_version` triples from `record_revisions`; load `from_version` triples from the previous revision, or re-expand the previous JSON-LD from `record_revisions.jsonld_zstd` when `from_version` has no revision row (bulk-loaded). If `payload.drift_check` and the stored `records.context_version` at bulk load differs from the current one, use `build_delete_where_subjects(owned_subjects)` instead of a diff. `kind='delete'` → delete-where all owned subjects.
   - Combine the batch into one update string (`;`-separated) and send it. On success mark events `done`, `processed_at`, and set `sync_state.qlever_index_watermark` to the max id.
   - On failure: `attempts += 1`, `last_error`, exponential backoff; after 5 attempts mark `failed` (dead letter) and continue with the next batch.
   - Expose counters via a tiny `/metrics` HTTP endpoint on the worker (`outbox_pending`, `outbox_failed`, `sync_lag_seconds`, `updates_sent_total`).
3. `lae sync-worker --once` processes one batch and exits (for tests and CI).
4. Integration tests (marked `integration`): publish → within 1 s the triple is in QLever; change a title → exactly one DELETE and one INSERT in the sent update (assert via a recording client); failed update ends in `failed` with the error text; worker restart resumes from pending.

**Verify:** `uv run pytest -m integration tests/sync -q` with compose services up.

**Commit:** `feat(sync): outbox worker with revision-diff updates, drift fallback, watermark, dead letters`

---

## Step 1.12 — Bulk bootstrap pipeline and AAT labels

**Do:**
1. `lae bootstrap load --input <dir-or-jsonl>`: streams Linked Art JSON-LD, assigns internal node IRIs, validates with JSON-Schema only (report failures to a file; skip them), writes `records` with `COPY` (`version=1`, `status='active'`, `draft_epoch=0`, `context_version=settings.context_version`) in batches of 10k, and `entity_labels` the same way. Record `bootstrap_context_version` and `bootstrap_expander_version` in `sync_state`.
2. `lae bootstrap export-nt --out <file.nt>`: multiprocess expansion of every record to N-Triples, with the sanitation checks, into one file (gzip optional). Also write sidecar labels as triples: for each `entity_labels` row with `source != 'local'`, emit `<uri> rdf:type la:<Type>` and `<uri> rdfs:label "label"@lang` into graph `urn:graph:sidecar:<source>`.
3. `lae bootstrap build-index --nt <file>`: runs `qlever index` with `deploy/Qleverfile` in a fresh directory, then `qlever start` with `--persist-updates`; sets `sync_state.qlever_index_watermark` to the max `outbox_events.id` at the time of the export (events after it are replayed by the worker).
4. `lae bootstrap aat-labels --csv /Users/rs2668/Development/linked-art/linkedart-parser/data/aat_dataset.csv`: inserts `entity_labels` rows (`uri = http://vocab.getty.edu/aat/<aat_code>`, `type='Type'`, `labels={"en": preferred_name}`, `default_label`, `source='aat'`, `status='published'`). This is the Phase 1 stand-in for the AAT sidecar.
5. Run the S6 synthetic generator at 1M records through all three commands and record timings in `docs/spikes/S6-bootstrap.md` (append a "Phase 1 actual" section).

**Verify:** Corpus + institutional sample + AAT labels loaded; `SELECT count(*) FROM records`; QLever answers `SELECT (COUNT(*) AS ?c) WHERE { ?s ?p ?o }`; a record published after the bootstrap appears in QLever via the worker.

**Commit:** `feat(bootstrap): COPY load, N-Triples export, index build, AAT label seed`

---

## Step 1.13 — Autocomplete API

**Do:**
1. `lae/search/autocomplete.py`: `GET /api/search/autocomplete?q=&types=Person,Group&limit=20&locale=`. Builds a QLever query per the GO result of Spike S1:
   - Prefix search with `ql:contains-word "<q>*"` over the text index (or `ql:has-word` on whole tokens when prefix is not supported), restricted to subjects with `rdf:type` in the requested classes, returning `?entity ?label ?type`, plus for `Person` the birth/death years and for `Place` the `part_of` parent label as disambiguation (`OPTIONAL` blocks).
   - If S1 recorded NO-GO for freshness, union the results with a Postgres query `SELECT uri, default_label, type FROM entity_labels WHERE updated_at > <watermark_time> AND default_label ILIKE $1 || '%'` so newly published entities appear until the next rebuild.
   - Escape `q` with the vendored `string_literal`. Reject `q` shorter than 2 characters.
2. Response: `[{id, type, label, context: str | null, source: "local"|"aat"|...}]`. Labels resolved through `hydrate_labels` with the caller's locale chain.
3. Tests: integration test after bootstrap that `q=rem` returns Rembrandt-like entities under 100 ms (measure and log).

**Verify:** `uv run pytest -m integration tests/search -q`.

**Commit:** `feat(search): QLever-backed autocomplete with disambiguation context`

---

## Step 1.14 — Revisions, semantic diff, and revert

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: the typed semantic diff is reused by publish, history, the graph overlay, and the rebase modal; its node-matching rules must be exact.


**Do:**
1. `lae/diff/semantic.py`: `semantic_diff(old: dict, new: dict) -> list[Change]` over projected JSON-LD (internal IRIs present). `Change` is one of `add_node {path, node_type, node_id, summary_fields}`, `remove_node {...}`, `update_literal {path, old, new}`, `add_ref {path, uri, type}`, `remove_ref {path, uri, type}`, `reorder {path}`. Match nodes by internal IRI, references by URI. `path` uses the profile path syntax and the first `classified_as` label as a qualifier where present (e.g. `identified_by[Name:Primary Name].content`).
2. Endpoints: `GET /api/records/{id}/revisions` → `[{version, created_at, authors, published_by, summary}]`; `GET /api/records/{id}/revisions/{n}` → stored JSON-LD; `GET /api/records/{id}/revisions/{m}/diff/{n}` → changes; `POST /api/records/{id}/draft/diff` → changes between the published version and the live draft snapshot (used by the Publish dialog).
3. `POST /api/records/{id}/revisions/{n}/revert` (requires `publish`): opens or replaces the draft with revision `n`'s JSON-LD (`hydrate` into the room via `crdt-ws` `close(discard)` then `open` with the historical content). The editor then publishes normally with summary "Revert to revision n".
4. Tests with hand-made before/after documents for every change kind.

**Verify:** `uv run pytest tests/diff tests/api/test_revisions.py -q`.

**Commit:** `feat(revisions): revision listing, typed semantic diff, revert-to-draft`

---

## Step 1.15 — Scheduler and the draft reaper

**Do:**
1. `lae/scheduler/main.py`: an `apscheduler` (or a simple asyncio loop) running registered jobs with `last_run`, `last_status`, `duration` stored in `sync_state` under `job:<name>`.
2. `lae/scheduler/reaper.py`: hourly. For every `crdt_drafts` row with `last_activity_at < now() - draft_ttl_days`: if `records.version >= 1` delete the draft rows; else delete the draft rows **and** the record row. Call `crdt-ws` `close(discard=True)` first. Log each action with record id and the contributors.
3. `GET /api/drafts` (requires `edit_draft`): list of open drafts with `record_id`, label, contributors, `last_activity_at`, `expires_at` (the Active Drafts Inbox UI arrives in Phase 2; the API is needed now for the reaper test and the editor's "your draft expires in N days" notice). `POST /api/records/{id}/draft/extend` resets `last_activity_at`.
4. Tests: a draft older than the TTL on a published record is removed; on an unpublished record the record is removed; extend prevents it.

**Verify:** `uv run pytest tests/scheduler -q`.

**Commit:** `feat(scheduler): job runner and 5-day draft reaper`

---

## Step 1.16 — Frontend foundation

**Do:**
1. Generate the API client: `pnpm openapi-typescript http://localhost:8000/openapi.json -o src/api/schema.d.ts`; `src/api/client.ts` wraps `fetch` with credentials and typed paths; TanStack Query hooks per resource in `src/api/hooks/`.
2. `src/app/`: router (`/`, `/edit/:id`, `/records/:id/history`, `/login`), `AuthProvider` reading `/auth/me`, a `RequirePermission` wrapper, an app shell with header, and a 404 page. All text through i18n keys in `common` and `editor`.
3. `src/i18n/formatters.ts`: `formatDate`, `formatRelative`, `formatNumber` using `Intl` with the active locale.
4. E2E: Playwright logs in with the dev user (set `LAE_DEV_USER` for the e2e API), opens `/`, runs `axe`.

**Verify:** `pnpm typecheck && pnpm lint && pnpm test && pnpm test:e2e`.

**Commit:** `feat(frontend): typed API client, router, auth provider, app shell`

---

## Step 1.17 — Frontend CRDT binding

**Do:**
1. `src/crdt/useRecordDoc.ts`: creates a `Y.Doc`, a `WebsocketProvider` from `y-websocket` to `/ws/rooms/{id}` with `params: {epoch}` (the epoch from `POST /api/records` or `GET /api/records/{id}` → add `draft_epoch` to that response), an `Awareness` with the user's identity, and a `Y.UndoManager` scoped to `trackedOrigins = new Set([localOrigin])` so undo only reverts the caller's edits. Returns `{doc, provider, undo, redo, connected, peers}`.
2. On close code `4409`: show a blocking dialog `editor.stale_epoch` with a "Reload" action (the full rebase modal is Phase 4). Keep the local doc in `IndexedDB` via `y-indexeddb` so it is not lost.
3. `src/crdt/paths.ts`: the TypeScript mirror of `lae/projection/ops.py`: `getPath`, `setScalar`, `addNode` (mints `<recordUri>/node/<uuidv7>` client-side with the `uuid` package's v7), `addReference`, `removeEntry`, `moveEntry` with fractional `_order`. Unit tests mirror the Python tests (same fixtures, copied from `backend/tests/projection/fixtures/`).
4. `src/crdt/useYPath.ts`: a hook subscribing to a path and re-rendering on change.
5. `src/crdt/RawInspector.tsx`: read-only drawer showing the projected JSON-LD (TypeScript `project()` mirror) refreshed at most once per second.

**Verify:** `pnpm test`; two browser windows on `/edit/:id` see each other's edits and presence.

**Commit:** `feat(crdt-client): useRecordDoc with scoped undo, path ops mirror, raw inspector`

---

## Step 1.18 — ProfileRenderer and core widgets

**Do:**
1. `lae/api/profiles.py`: `GET /api/profiles/{class}` serving the YAML from `profiles/ui/` as JSON (validated at startup). Write `Person.yaml`, `Place.yaml`, `Type.yaml` alongside the existing `HumanMadeObject.yaml`.
2. `profiles/vocab/phase1_static.yaml`: hand-written enumerated Value Sets for Name types, Identifier types, Statement types, Dimension types, Measurement Units (from the Linked Art model docs), served by `GET /api/vocab/bundle` as a single ETag-cached JSON. Phase 2 replaces the implementation but keeps the endpoint.
3. `src/editor/ProfileRenderer.tsx`: renders `sections` → `slots`; `advanced` sections collapsed behind "Add details"; each slot resolved through `src/editor/widgets/registry.ts` mapping `widget` names to components.
4. Widgets in `src/widgets/`: `TextWidget`, `LangTextWidget` (content `Y.Text` bound to a textarea, BCP-47 language select, `dir` select with `auto` default, Name type from the bundle), `EdtfWidget` (uses the `edtf` npm package to parse into `begin_of_the_begin` / `end_of_the_end` and display label; shows parse errors inline), `EntityRefAutocomplete` (autocomplete against `/api/search/autocomplete` with `types` from the slot, shows disambiguation context, "Create new …" opens `CreateStubDialog` which calls `POST /api/records` and inserts the reference), `ClassificationWidget` (pills from the Value Set; unknown values rendered in an "Other classifications" group with remove only), `DimensionWidget`, `IdentifierWidget`, `StatementWidget`, `ProductionWidget` (carried_out_by refs + EDTF + place ref over `produced_by`).
5. `src/editor/EditorPage.tsx` (`/edit/:id`): loads the record, the profile, the bundle; renders `ProfileRenderer`, presence avatars, undo/redo buttons, the validation drawer and publish button (Step 1.19), the raw inspector toggle.
6. Every widget: keyboard operable, labels via `<label for>`, 24 px targets, no literal strings. Unit tests for `LangTextWidget`, `EdtfWidget`, `EntityRefAutocomplete`.

**Verify:** Open a corpus `HumanMadeObject` in `/edit/:id`, edit title, materials, dates, creator; the raw inspector shows exactly the expected JSON-LD; `pnpm test:e2e` axe passes on `/edit/:id`.

**Commit:** `feat(editor): ProfileRenderer, widget registry, core widgets, editor page`

---

## Step 1.19 — Validation drawer and Publish dialog

**Do:**
1. `src/editor/ValidationDrawer.tsx`: "Validate" calls `POST /api/records/{id}/draft/validate`; also runs Ajv client-side on every change (debounced 500 ms) with the same schema from `/api/profiles/schema/{class}` (add that endpoint, serving `profiles/schema/`). Messages come from `errors.json` by code with `params` interpolated via ICU. Clicking a diagnostic scrolls to and focuses the slot whose path matches.
2. `src/editor/PublishDialog.tsx`: requires `publish`; shows the semantic diff from `POST /api/records/{id}/draft/diff` rendered per change kind via i18n keys (`editor.diff.add_ref` etc.), a summary input, a checkbox per warning, and the Publish button (disabled while violations exist). On success, navigate to `/id/{uuid}` in a new tab and reload the editor (which now opens a fresh draft lazily on first edit).
3. `src/editor/DiscardButton.tsx` for Editors.
4. Aria-live region announces validation summary and publish result only.

**Verify:** Publish a record end to end from the UI; `curl -H 'Accept: application/ld+json' /id/<uuid>` shows the change; QLever shows the new triple.

**Commit:** `feat(editor): validation drawer with localized diagnostics and publish dialog with semantic diff`

---

## Step 1.20 — Revision History Browser

**Do:**
1. `src/editor/HistoryPage.tsx` (`/records/:id/history`): revision list, select two revisions → diff rendered with the same change components as the Publish dialog, "View" opens `/id/{uuid}?version=N`, "Revert to this revision" (Editors) calls the revert endpoint and opens the editor.
2. E2E: publish twice, open history, diff shows the title change, revert creates a draft equal to revision 1.

**Verify:** `pnpm test:e2e`.

**Commit:** `feat(editor): revision history browser with diff and revert`

---

## Step 1.21 — Public Reader v1

**Do:**
1. `lae/reader/templates/record.html` (Jinja2, server-rendered, no React): `<html lang dir>` from the locale chain; title = default label in the locale; sections for identifiers, classifications, production (creator, date, place), materials, dimensions, statements, references (links to `/id/`); links to JSON-LD and Turtle; a `<canvas-panel>` element when the record has `representation` → `digitally_shown_by` → `access_point` pointing at a IIIF resource (Spike S5 rules); an "Edit" link to `/edit/{uuid}` shown only when a session cookie is present; version banner when `?version=N`.
2. Labels hydrated through `hydrate_labels`; dates formatted server-side with `babel` for the locale.
3. Axe check on `/id/{uuid}` in the e2e suite; Lighthouse performance budget: first contentful paint under 1 s locally without JS.

**Verify:** `curl -H 'Accept: text/html' /id/<uuid>` renders; axe passes; no JavaScript bundle from the SPA is loaded on the reader.

**Commit:** `feat(reader): server-rendered public record page with Canvas Panel`

---

## Step 1.22 — Scale and collaboration gate

**Do:**
1. `scripts/load/bootstrap_1m.sh`: runs the S6 generator and the three bootstrap commands; records timings into `docs/spikes/S6-bootstrap.md`.
2. `scripts/load/publish_storm.py`: 20 concurrent publishes per second for 2 minutes against the running stack; record sync lag p95 from the worker `/metrics` and QLever delta count.
3. `frontend/e2e/collab.spec.ts`: three browser contexts edit the same record (title, a material, a dimension) for 60 seconds with overlapping edits, then one publishes; assert the published JSON-LD contains all three edits and the corpus round-trip invariant holds for the result.
4. Autocomplete latency: 200 random prefixes against the 1M load; record p95.
5. Write `docs/spikes/P1-exit-measurements.md` with all numbers.

**Verify:** The numbers meet the Phase 1 exit criteria in `docs/implementation_phases.md` (publish-to-QLever p95 < 1 s, autocomplete p95 < 100 ms, no data loss in the collaboration test).

**Commit:** `test: phase 1 scale and collaboration gate`

---

## Exit Checklist

- [ ] `tests/test_roundtrip.py` passes for 100% of the corpus through `hydrate` → `project` → `strip_internal_iris`.
- [ ] Publish from the UI writes `records`, `record_revisions`, `entity_labels`, `outbox_events` in one transaction and the change is in QLever within 1 s (measured).
- [ ] Dependent stub created inline publishes atomically with its parent.
- [ ] A stale-epoch client is rejected with close code 4409 and keeps its local edits in IndexedDB.
- [ ] Draft reaper removes a 6-day-old draft and an unpublished 6-day-old record.
- [ ] `/id/{uuid}` serves JSON-LD (with profile), Turtle, HTML, `?version=N`, 410 for deleted, with no internal node IRIs in any body.
- [ ] Autocomplete returns newly published entities (either natively or via the overlay) and p95 < 100 ms at 1M records.
- [ ] 1M-record bootstrap completed with timings recorded.
- [ ] Three-editor collaboration e2e passes with no lost edits.
- [ ] `errors.json` has an entry for every diagnostic code; `pnpm lint` reports zero literal strings; axe passes on `/`, `/edit/:id`, `/records/:id/history`, `/id/:uuid`.
- [ ] `docs/spikes/P1-exit-measurements.md` exists.

## Common Mistakes

- Expanding a record **with** foreign stubs' `type` and emitting `rdf:type` for other records' subjects. Strip to `{"id"}` first.
- Validating against the JSON Schema **without** stripping internal node IRIs (the schema rejects `id` on embedded nodes) or running SHACL **with** them stripped (paths then cannot be mapped back).
- Broadcasting a CRDT update before persisting it to `crdt_updates`. A crash then loses it.
- Caching QLever query results in the client. The editor reads immediately after writing.
- Computing `old_triples` for a bulk-loaded record's first edit from the current context when the bootstrap used an older one. Check `drift_check` and use `DELETE WHERE`.
- Letting the Publish dialog publish with warnings that were never shown as checkboxes. Every warning must be explicitly accepted.
- Writing `_label` into `records.data` for references that exist in `entity_labels`.
- Mirroring `ops.py` in TypeScript with different `_order` arithmetic. Use the shared fixtures; both sides must produce identical projections.
