# Decision Register

Every decision that an implementer might otherwise have to make again, in one place. **Status:** `locked` (changing it needs a human and an ADR), `fixed` (a tooling/naming choice made in the guides; change only with an ADR), `open` (not yet decided; see the named spike or step). "Source" points at the authoritative text.

Decisions made during the November 2026 design review are also written up as ADRs `0001`–`0017` in this directory (ADR-0018 records the refinements found by Spike S3; ADR-0021 the ownership rule for embedded nodes that have their own ids; ADR-0024 the URIs of bulk-loaded records); the register links them.

## A. Architecture and storage

| ID | Decision | Status | Source |
| :-- | :-- | :-- | :-- |
| A1 | PostgreSQL is the single system of record; QLever is a derived index rebuilt from Postgres. | locked | spec §1, §2 |
| A2 | All mutations commit to Postgres first; a transactional outbox (`outbox_events`, `LISTEN/NOTIFY` + 1 s poll) feeds a sync worker that updates QLever. | locked | spec §2, §4.1; Phase 1 Step 1.11 |
| A3 | Processes: `api`, `crdt-ws`, `sync-worker`, `scheduler`, `embedder`, `reconcile`, `llm`, `qlever`, `postgres`. Same names for Procfile entries, systemd units, and `lae` CLI sub-commands. | locked | spec §4.2; conventions §1; ADR-0011 |
| A4 | Validate and publish requests needing the live Y.Doc are routed from `api` to `crdt-ws` over an internal HTTP RPC (`/internal/...`, header `X-Internal-Token`). `crdt-ws` is a single process. | locked | spec §4.2; Phase 1 Step 1.8; ADR-0011 |
| A5 | Single tenant per installation; fully open published data; no field-level redaction. | locked | spec §1, §8 |
| A6 | Record ids are UUIDv7; public URIs are flat `/id/{uuidv7}`; no class-typed paths. A record bulk-loaded with a URI from elsewhere keeps that URI as `records.uri` and gets a UUIDv7 `id` (ADR-0024, owner to confirm). | locked | spec §3.1; ADR-0024 |
| A7 | Embedded nodes get internal IRIs `<record_uri>/node/<uuidv7>`; stripped from public JSON-LD; visible in SPARQL results; `/id/{uuid}/node/{id}` 303-redirects to the record. In RDF only, an embedded node that arrived with an `id` of its own gets the derived IRI `<record_uri>/node/<uuidv5(original id)>` and a `la:equivalent` triple to that id; the whole `<record_uri>/node/` namespace belongs to the record. | locked | spec §3.3; ADR-0003; ADR-0021 |
| A8 | `records.data` is JSONB with TOAST `lz4`; revision N-Triples and JSON-LD snapshots are application-level zstd (level 9) `bytea`. | locked | spec §3.1; Phase 1 Step 1.3 |
| A9 | Every edited revision stores canonical N-Triples (sorted, deduplicated lines) and a JSON-LD snapshot. Bulk-loaded records have no revision row until first edited. | locked | spec §3.1, §3.3 |
| A10 | `entity_labels` (uri, type, BCP-47 `labels` JSONB, `default_label`, `metatype_uris[]`, `vocab_ids[]`, `source`, `status`, `usage_count`) is the single label lookup for local and sidecar entities; `_label` is transcluded at read time. | locked | spec §3.1, §3.2; Phase 1 Step 1.5; Phase 2 Step 2.1 |
| A11 | `_label` is stripped at write time **except** for references whose target has no `entity_labels` row; those keep `_label` and raise an unresolved-reference QA finding. | locked | spec §3.2; ADR-0006 |
| A12 | Foreign references are reduced to `{"id"}` before expansion so no `rdf:type` or label triples are emitted for subjects the record does not own. An embedded node with a foreign `id` is not a reference: its content is emitted under a derived node IRI (A7). Which is which is decided from the object alone by the rule in ADR-0021. | locked | spec §3.3; ADR-0021; Phase 1 Step 1.3 |
| A13 | Sync uses revision-diff `DELETE DATA` / `INSERT DATA`. For the first edit of a bulk-loaded record whose bootstrap context/expander version differs from the current one, use `DELETE WHERE { <s> ?p ?o }` over owned subjects instead. | locked | spec §3.3; ADR-0007 |
| A14 | QLever runs with `--persist-updates`; `sync_state.qlever_index_watermark` records the last applied outbox id; rebuilds are blue/green (snapshot at watermark, build in a new directory, replay, atomic swap, keep previous for rollback), triggered by delta-triple threshold or on demand, never by editor idleness. | locked | spec §3.3; Phase 2 Step 2.18; ADR-0002 |
| A15 | QLever is installed natively (Homebrew on macOS; native packages or a source build from `lux/qlever-dev` on Linux) at the version in `deploy/QLEVER_VERSION`; Qleverfile uses `SYSTEM = native`, `TEXT_INDEX = from_literals` and `ADD_HAS_WORD_TRIPLES = true`. | fixed | reuse_map; Phase 0 Step 0.3 |
| A16 | Autocomplete, backlinks, delete protection, merge referrer discovery, and the Tier 2 backlink guardrail use QLever (not a Postgres edge table). A Postgres `entity_labels` overlay is used only if Spike S1 shows text-index staleness. | locked | ADR-0001; Phase 1 Step 1.13 |
| A17 | External authorities are local sidecar datasets in `urn:graph:sidecar:<id>` graphs plus `entity_labels` rows; no live external calls during editing; the existing LUX Wikidata subset is the Wikidata source; no on-demand single-entity import in v1. | locked | spec §3.2, §10.5; ADR-0009 |
| A18 | Drafts are never indexed in QLever and are not searchable; new unpublished stubs have no `entity_labels` row until published. | locked | spec §4.1; ADR-0010 |
| A19 | Vector search lives in Postgres `pgvector` (`halfvec`, HNSW `m=16, ef_construction=128`), transactional with `entity_labels`; no third vector store. | locked | spec §3.1, §6.3; Phase 3 Step 3.1 |
| A20 | The `/sparql/vector` SPARQL `SERVICE` contract is the LUX `sparql_service` `vec:` vocabulary ported to pgvector; interactive autocomplete uses application-layer RRF. Final confirmation in Phase 3 Step 3.3. | open → leading option | spec §14 Open Question 1; ADR-0016 (Proposed) |
| A21 | `/id/{uuid}`: `?format=jsonld\|ttl\|html` overrides `Accept` (default HTML); weak per-representation `ETag` `W/"<records.etag or snapshot hash>-<format>"` with `Vary: Accept, Accept-Language`; status (410/301) is checked before `?version=N`; version N is its revision row, else `records.data` if N is current, else 404. `records.etag` = BLAKE2b-128 of the canonical JSON. `POST /api/records` accepts the top-level Linked Art classes only and stores a `template` in `records.data` for the room to hydrate. | fixed | Phase 1 Step 1.6; ADR-0023 |

## B. Editing, CRDT, publishing

| ID | Decision | Status | Source |
| :-- | :-- | :-- | :-- |
| B1 | Yjs in the browser, `pycrdt` + `pycrdt-websocket` on the server, behind a `RoomManager` Protocol so a Yjs-wire-compatible sidecar is a fallback. | locked | spec §4.2; Phase 1 Step 1.8 |
| B2 | Root `Doc` has one `Map` named `record`; array items are keyed map entries, never positions in a `Y.Array`: items that are nodes are keyed by internal node IRI and items that are references by target URI, each stored in the `Map` of the object that has the property under the key `[property, item key]` (no separate `Map` per array, so concurrent first inserts are all kept; repeats within one array get a ` #n` suffix and are preserved); `content` on `Name`, `Identifier`, `LinguisticObject` is `Text`; other scalars are plain values, with numbers and untyped JSON kept exact by the `_json` encoding; unknown keys are preserved verbatim. The exact rules are R1–R19 of `docs/spikes/S3-projection.md`. | locked | spec §4.2; ADR-0018; `docs/spikes/S3-projection.md`; Phase 1 Step 1.4 |
| B3 | Every keyed entry carries a fractional-index `_order` (floats; initial values 0.0, 1.0, …; inserts take midpoints; an exhausted gap or a tie renumbers that property's entries); projection sorts by `_order` then entry key in Unicode code-point order; `_order` never appears in JSON-LD. | locked | spec §4.2; ADR-0005; ADR-0018 (rules R12, R13) |
| B4 | One shared live draft per record; Collaborators (`edit_draft`) co-edit; only Editors (`publish`) publish or discard. | locked | spec §4.3 |
| B5 | Every incoming CRDT update is persisted to `crdt_updates` **before** broadcast; compaction into `crdt_drafts.state` every 200 updates or on eviction; rooms evict after 10 minutes idle. | fixed | Phase 1 Step 1.8 |
| B6 | `records.draft_epoch` increments on every publish; a client with an older epoch is closed with WebSocket code `4409` reason `stale-epoch`; v1 rebase modal is a per-change Apply/Discard list (no automatic three-way merge). | locked | spec §4.2; Phase 1 Step 1.17; Phase 4 Step 4.15 |
| B7 | Drafts with no activity for 5 days (setting `draft_ttl_days`) are discarded; unpublished new records are deleted. Hourly reaper in `scheduler`. | locked | spec §4.1; Phase 1 Step 1.15 |
| B8 | Publish is one Postgres transaction: validate all documents (primary + unpublished dependent stubs), lock rows in UUID order, write `records`, `record_revisions`, `entity_labels`, `outbox_events`, delete draft rows; then close rooms. Warnings must be explicitly accepted by code+path. | locked | spec §4.1; Phase 1 Step 1.10 |
| B9 | Revisions are append-only; "revert" hydrates revision N into the draft and goes through normal publish. | locked | spec §4.3; Phase 1 Step 1.14 |
| B10 | Semantic diff is a list of typed changes (`add_node`, `remove_node`, `update_literal`, `add_ref`, `remove_ref`, `reorder`) matched by internal IRI / target URI, never prose. | locked | spec §4.3; Phase 1 Step 1.14 |
| B11 | User-scoped undo via `Y.UndoManager` tracked origins; agent turns are undone via `RoomManager.revert(transaction_ids)`. | locked | spec §6.5, §6.8 |
| B12 | All draft-scoped endpoints use `/api/records/{id}/draft/...`; `apply_change` (`lae.crdt.ops`) is the single generic write path for typed changes from agents, inverse links, smart paste, and rebase. | fixed | Phase 1 Step 1.10; Phase 3 Step 3.6 |

## C. Validation and vocabularies

| ID | Decision | Status | Source |
| :-- | :-- | :-- | :-- |
| C1 | JSON-Schema (vendored from `linked-art/json-validator`, Draft 7) is the first-order gate; project-authored SHACL shapes (`profiles/shapes/*.ttl`, each constraint with `lae:code`) run after it; cross-field Python rules cover what SHACL handles poorly. | locked | spec §5; ADR-0008; Phase 1 Step 1.9 |
| C2 | Severities: `violation` blocks publish; `warning` requires explicit acknowledgement; `info` is UI guidance. | locked | spec §5 |
| C3 | Diagnostics are structured `{code, path, severity, params}` with codes `LAE-<AREA>-<NNN>` (schema codes `LAE-SCHEMA-<validator>`); English messages live only in `errors.json`. Expansion failures are `LAE-RDF-NNN` violations; SHACL results are located by JSON Pointer and never name an internal node IRI. | fixed | conventions §2; Phase 1 Step 1.9; ADR-0025 |
| C4 | Value Sets: `enumerated` (ordered term lists, shipped in an ETag-cached boot bundle) or `rule` (metatype / broader roots / exclusions / classes, resolved in QLever into `vocabulary_membership`). Enforcement `strict`/`extensible`/`advisory` compiles into validation rules. Bindings are `(entity_class, property_path, vocabulary)`; several Value Sets may bind to one path (each is a card), and a value is checked only against the bindings that claim it (membership or metatype). | locked | spec §7; Phase 2 Steps 2.1–2.2 |
| C5 | `classified_as` values not claimed by any bound Value Set render in an "Other Classifications" bucket and are never dropped. | locked | spec §6.1, §7.2 |
| C6 | All human-readable config strings are BCP-47 `LangMap`s with `en` required; resolution chain is user preference → localStorage → Accept-Language → `en`. | locked | spec §6.1, §6.7 |
| C7 | Value Set definitions and bindings are YAML in `profiles/vocab/` with import/export CLI; the registry is the single source for pickers, validation, drag-and-drop targeting, and MCP resources. | locked | spec §7.3, §7.4 |

## D. UI and workbench

| ID | Decision | Status | Source |
| :-- | :-- | :-- | :-- |
| D1 | Frontend is TypeScript + React 19 + Vite + pnpm; TanStack Query for data; TanStack Table for grids; Tailwind with logical utilities only. | fixed | spec §11; Phase 0 Step 0.1 |
| D2 | UI Profiles are YAML per class in `profiles/ui/` (schema in `profiles/ui/_schema.json`): sections → slots → widgets, with `path`, `widget`, `value_set`, `multiple`, `required`, `advanced`. Widget names: `text`, `langtext`, `edtf`, `entity_ref`, `classification`, `dimension`, `identifier`, `statement`, `wkt`, `production`. | fixed | Phase 0 Step 0.12; Phase 1 Step 1.18 |
| D3 | Phase 1 profiles: `HumanMadeObject`, `Person`, `Place`, `Type`; Phase 2 adds `Group`, `VisualItem`, `LinguisticObject`, `Set`, `Activity`/`Event`. | fixed | implementation_phases |
| D4 | IIIF viewer is Canvas Panel (Digirati web component) for all media, including A/V; Clover and Mirador are not used. Gaps found by Spike S5 go to the owner, not to another viewer. | locked | ADR-0012; spec §6.2 |
| D5 | Map authoring is MapLibre GL JS + Terra Draw; geometry stored as one LWW WKT value; raw WKT textarea and `.geojson` drop are the accessible equivalents. | locked | spec §6.2 |
| D6 | Drag-and-drop uses `@dnd-kit` with HTML5 fallback and MIME `application/x-linked-art-ref+json` plus `text/uri-list`; every drag has a keyboard "Link to slot…" equivalent. | locked | spec §6.4, §6.9 |
| D7 | Force graph is Sigma.js + Graphology; mini-graph is React Flow (`@xyflow/react`); diagrams are client-side Mermaid; all explorers consume one `ResultSet` contract. | fixed | spec §6.6; Phase 4 Steps 4.1–4.3 |
| D8 | Public Reader is server-rendered (Jinja2) at `/id/{uuid}`; the React editor loads only on Edit. | locked | spec §6.6, §12; Phase 1 Step 1.21 |
| D9 | WCAG 2.2 AA throughout; axe + Playwright on every route; `aria-live` only for the listed events; targets ≥ 24×24 px. | locked | spec §6.9 |
| D10 | i18n: `react-i18next` + `i18next-icu`, namespaces `common`, `editor`, `reader`, `graph`, `vocab_admin`, `errors`, `admin`; `no-literal-string` lint as error; pseudo-locale in dev; ship `en` only at first. | locked | spec §6.7; Phase 0 Step 0.2 |
| D11 | SPA routing is `react-router` (data router, one `routes` table in `src/app/routes.tsx`); the API client is `openapi-fetch` over `src/api/schema.d.ts`, generated offline from `lae.app.openapi_document()` (`corepack pnpm api:generate`) and checked for staleness in CI (`api:check`); non-2xx responses throw `ApiError` with the backend's `{code, path, params}`. | fixed | ADR-0022; Phase 1 Step 1.16 |

## E. Security, agents, LLMs

| ID | Decision | Status | Source |
| :-- | :-- | :-- | :-- |
| E1 | OIDC via Authlib for humans; PATs (SHA-256 stored) and client credentials for programs; roles Viewer, Collaborator, Editor, Vocabulary Admin, System Admin, Tier 1 Agent, Tier 2 Agent with the permission names in spec §8.2. | locked | spec §8; Phase 1 Step 1.7 |
| E2 | `LAE_DEV_USER` bypass exists for development and is refused when `LAE_ENV=production`. | fixed | Phase 1 Step 1.7 |
| E3 | All outbound HTTP goes through `lae.net.egress` with an SSRF allow/deny list; QLever's own outbound SERVICE stays enabled. | locked | spec §10.5; ADR-0013 |
| E4 | Public QLever access is only through the read-only `/sparql` proxy (timeouts, rate limits, update keywords rejected); the update endpoint is internal-only. | locked | spec §8.3; Phase 2 Step 2.17 |
| E5 | MCP server on MCP Python SDK v2, stateless Streamable HTTP at `/mcp`; agents authenticate as service accounts; every draft mutation goes through `RoomManager`; every call is audited. | locked | spec §9; Phase 3 Steps 3.4–3.6 |
| E6 | Long-running agent work uses the server-owned `agent_jobs` table (not the MCP Tasks extension). | locked | ADR-0004; Phase 3 Step 3.7 |
| E7 | Cross-record merges from agents go through `curation_proposals` with lease claiming, a negative cache (`merge_blocklist`), server-enforced blast-radius guardrails, quotas, and a circuit breaker; humans resolve `needs_human_review`. | locked | spec §3.4, §9; Phase 3 Step 3.8 |
| E8 | Chat tool execution runs under `user_perms ∩ agent_tier_perms`; high-impact tools require in-chat confirmation; each turn is revertible. | locked | spec §6.8, §8.2; Phase 3 Step 3.13 |
| E9 | LLM access is through one adapter built on LiteLLM functions copied from chai-engine, with `litellm` pinned to an exact version; provider keys are envelope-encrypted (`AES-256-GCM` DEK wrapped by `LAE_MASTER_KEY`); `local_only` vs `allow_cloud` policy; optional per-user BYOK. | locked | spec §9; Phase 3 Step 3.12 |
| E10 | Reconciliation is on demand only, via the new `lae-reconcile` process built on the LUX LMDB reconciler with network off, returning candidates with evidence. | locked | ADR-0014; Phase 3 Step 3.11 |
| E11 | Data translation is additive only (`Name.content`, `LinguisticObject.content`), appends a sibling node with `AttributeAssignment` provenance and `source_content_hash`; Public Reader translations are ephemeral and never enter records or QLever. | locked | spec §6.7; Phase 4 Steps 4.10–4.11 |
| E12 | Session is a signed `SessionMiddleware` cookie `lae_session` (`HttpOnly`, `SameSite=Lax`, `Secure` outside development, 12 h idle) reloaded against `users` on every request; migration `0002` seeds all seven roles (Vocabulary Admin = `manage_vocabularies`); new users get Viewer; PAT permissions = scopes ∩ current role permissions, tokens minted only from a session or the dev user; bearer token > dev user > session; 401/403 bodies `{code, path, params}` with `LAE-AUTH-NNN`. | fixed | ADR-0020; Phase 1 Step 1.7 |
| E13 | Several sign-in providers at once: `oidc` and `github` (GitHub OAuth App via Authlib's OAuth 2.0 client; `state`, PKCE `S256`, scopes `read:user user:email`, plus `read:org` with an org allowlist). `GET /auth/providers`, `GET /auth/login/{provider}`, `GET /auth/callback/{provider}`; `/auth/login` and `/auth/callback` serve the default (first enabled) provider; `oidc` keeps `/auth/callback` as its redirect URI. GitHub users are `subject = "github:<numeric id>"`, `email` only from a verified primary address, never linked by e-mail; optional `LAE_GITHUB_ALLOWED_USERS` / `LAE_GITHUB_ALLOWED_ORGS` (403 `LAE-AUTH-012`), else any GitHub user signs in as Viewer. | fixed | ADR-0026; Step 1.7 follow-up |

## F. Tooling, naming, process

| ID | Decision | Status | Source |
| :-- | :-- | :-- | :-- |
| F1 | Monorepo layout `backend/`, `frontend/`, `profiles/`, `deploy/`, `scripts/`, `tests/corpus/`, `docs/`; Python package `lae` (src layout). | fixed | conventions §1 |
| F2 | Python 3.12, `uv`, `hatchling`, FastAPI, SQLAlchemy 2 async + `asyncpg`, Alembic, `pydantic-settings` (`LAE_` prefix), `structlog`, `orjson`, `zstandard`, `uuid-utils`, `pyld` (expansion), `pyoxigraph` (Turtle), `pyshacl`, `jsonschema` + `referencing`, `httpx[http2]`, `apscheduler` or asyncio loop for the scheduler. | fixed | Phase 0 Step 0.1; Phase 1 |
| F3 | Lint and types: `ruff` (line 100), `mypy --strict`; tests: `pytest` + `pytest-asyncio` (auto), `hypothesis`; tests run against a native PostgreSQL via `LAE_TEST_DATABASE_URL` locally and in CI. | fixed | Phase 0 Step 0.1 |
| F4 | Migrations numbered by phase: 0001–0009 (Phases 0–1), 0010–0019 (2), 0020–0029 (3), 0030–0039 (4); every migration has a downgrade; merged migrations are never edited. | fixed | conventions §3 |
| F5 | Commit format `<type>(<scope>): <summary>`; one guide step per PR; PR names the step. | fixed | conventions §7 |
| F6 | Nothing from adjacent repos is imported as a package; code is vendored with a `# Vendored from <repo>/<path> @ <commit>` header and tests. | fixed | conventions §8; reuse_map |
| F7 | The golden corpus is the 211 linked.art examples plus ≥20 institutional records; 100% round-trip is a hard gate from Phase 1 on. | locked | Phase 0 Step 0.4; Phase 1 Step 1.4 |
| F8 | Default implementer is Claude Opus 5.5; steps marked **CRITICAL** are implemented and reviewed by a model from the tier above (Claude Fable/Mythos class). | fixed | ADR-0015; conventions §10 |
| F9 | Spikes S1–S6 produce written GO/NO-GO reports in `docs/spikes/`; a NO-GO stops the phase until a human acknowledges. | fixed | Phase 0 |
| F11 | No Docker or containers anywhere: development, CI, and production install PostgreSQL, QLever, and the Python processes natively. | locked | ADR-0017 |
| F12 | The product is named **Laertes** (Linked Art Editor with Real Time Entity Synchronization) in all user-visible text; internal identifiers (`lae` package and CLI, `LAE_` settings, `lae-*` systemd units) are unchanged. | fixed | Owner, 2026-11-22 |
| F10 | Questions go to `docs/questions.md`; design changes go to `docs/decisions/NNNN-*.md`; the spec is never edited to match code. | fixed | conventions §8 |
