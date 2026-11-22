# Phase 3 — Semantic Search & AI Agents: Step-by-Step Guide

**Read first:** `docs/README.md`, `docs/conventions.md`, `docs/glossary.md`, `docs/spec/` (section numbers are preserved from the original design document) sections 3.1, 6.3, 6.5, 6.8, 8.2, 9, 10.2, `docs/reuse_map.md`, and the Phase 1–2 guides (you must know `lae.crdt.rooms.RoomManager`, `lae.validation.validate`, `lae.projection`, `lae.vocab`, `lae.curation.merge`, and `/api/search/autocomplete`).
**Goal:** Hybrid lexical + semantic search over `pgvector`, and AI agents that work inside the same CRDT rooms, validation, and publish gates as humans, under enforced governance, with a human review queue for merges.
**You are done when** every item in the Exit Checklist at the bottom passes.

Rules for this phase:
- Agents never write to PostgreSQL records directly. Every draft mutation goes through `lae.crdt.rooms.RoomManager`, exactly like a browser client. If you find yourself importing `lae.db` from `lae.mcp.tools`, stop.
- No agent tool may mint a URI. Agents choose among candidates returned by `search_entities` / `reconcile_entity`, or call `create_draft_record`, which mints the UUIDv7 server-side.
- Every LLM call is recorded in `agent_audit_log` with provider, model, token counts, and cost before its result is used.
- Copy functions from `llm/chai-engine`; never `import chai`. It pulls in torch and loads `.env` at import time.
- Pin `litellm` to an exact version in `pyproject.toml` and add a comment explaining why (release 1.107.0 was compromised). Never use `>=`.
- Migrations in this phase are numbered `0020`–`0029`.

---

## Step 3.1 — Embedding worker and `entity_embeddings`

**Why:** Semantic autocomplete, the duplicate guard, and Tier 1 duplicate sweeps all need concept and authority embeddings that stay transactional with `entity_labels`.

**Do:**
1. Migration `0020_entity_embeddings.py`:
   - `entity_embeddings`: `uri (text pk, fk entity_labels.uri on delete cascade)`, `model_id (text)`, `dims (int)`, `embedding (halfvec(768))`, `text_hash (text)`, `updated_at`.
   - Index: `CREATE INDEX ON entity_embeddings USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 128)`.
   - `embedding_queue`: `uri (text pk)`, `reason (enum: publish, sidecar_reload, model_migration)`, `enqueued_at`.
   - Trigger: `AFTER INSERT OR UPDATE OF labels, default_label ON entity_labels` → insert into `embedding_queue` **only** when `type` is in (`Type`, `Material`, `Language`, `MeasurementUnit`, `Currency`, `Place`, `Person`, `Group`).
2. `backend/src/lae/embed/encoder.py`: vendor `lux/qlever-middletier/lux_embed/encoder.py`. Keep the model table (EmbeddingGemma 2 at 768d default; Harrier 0.6b optional). Expose `Encoder.encode(texts: list[str]) -> np.ndarray` and `Encoder.model_id`.
3. `backend/src/lae/embed/cards.py`: vendor `lux_embed/linkedart.py` name/statement extraction. Add `build_card(record_or_sidecar_entity: dict, labels: dict) -> str` returning `preferred label | alt labels | scope note | broader chain labels`, which is the text that is embedded (spec 3.1).
4. `backend/src/lae/embed/worker.py`: `lae embedder` CLI command. Loop: `SELECT ... FROM embedding_queue FOR UPDATE SKIP LOCKED LIMIT 256`, build cards, encode in one batch, upsert `entity_embeddings`, delete from the queue, all in one transaction. Also serve `POST /internal/embed` (`{"texts": [...]}` → `{"model_id", "vectors"}`) on an internal port for query-time encoding, with a warm model loaded at start.
5. `backend/src/lae/embed/client.py`: `EmbedClient.encode_query(text) -> list[float]` calling `/internal/embed` with a 200 ms timeout; on timeout raise `EmbedUnavailable`.
6. Add `embedder` to `deploy/docker-compose.yml` under profile `ai` with the model cache volume. Keep torch out of the `api` image: `backend/pyproject.toml` optional dependency group `embed = ["torch==…", "sentence-transformers==…"]` and a test `tests/test_import_boundaries.py` asserting `import lae.api` does not import `torch` (copy the pattern from `lux/qlever-middletier/tests/test_import_boundaries.py`).
7. `lae embedder --backfill`: enqueue every eligible `entity_labels` row.

**Verify:** `uv run pytest tests/embed -q` (encoder mocked). Run `make up-ai`, `lae embedder --backfill`, and confirm `SELECT count(*) FROM entity_embeddings` equals the eligible `entity_labels` count. `curl /internal/embed` returns a 768-float vector in under 40 ms warm.

**Commit:** `feat(embed): entity_embeddings, queue trigger, embedder worker`

---

## Step 3.2 — Hybrid RRF autocomplete and Pre-Creation Duplicate Guard

**Why:** Catalogers must see conceptual matches ("round painting" → *tondo*) without losing fast prefix search.

**Do:**
1. `backend/src/lae/search/semantic.py`: `semantic_search(session, vector, *, classes: list[str] | None, vocab_ids: list[str] | None, metatype_uris: list[str] | None, k: int = 20) -> list[Hit]`. SQL: join `entity_embeddings` to `entity_labels`, filter with `type = ANY(:classes)`, `vocab_ids && :vocab_ids`, `metatype_uris && :metatype_uris`, order by `embedding <=> :vec`, `LIMIT :k`. Set `SET LOCAL hnsw.ef_search = 100`.
2. `backend/src/lae/search/fusion.py`: `rrf(lexical: list[Hit], semantic: list[Hit], k: int = 60) -> list[FusedHit]`. Score = Σ 1/(k + rank). Each `FusedHit` carries `sources: set["lexical","semantic"]`; set `semantic_only = sources == {"semantic"}`.
3. Extend `GET /api/search/autocomplete` (Phase 2) with `mode=hybrid` (default for `rule` Value Sets and open entity slots; `lexical` for `enumerated` sets). Run the QLever lexical query and, in parallel, `EmbedClient.encode_query` + `semantic_search`. On `EmbedUnavailable`, return lexical results with header `X-LAE-Degraded: embed`.
4. `POST /api/records/duplicate-check`: body `{type, labels: {lang: text}, identifiers?: [str], equivalent?: [uri]}`. Returns candidates from (a) exact `equivalent` / identifier match in `entity_labels` or `records`, (b) QLever label search, (c) semantic search; each with `reason`. Used before a UUIDv7 is minted.
5. Frontend: `frontend/src/widgets/EntityRefAutocomplete.tsx` shows a `Semantic match` badge when `semantic_only` (i18n key `editor:autocomplete.semantic_match`). `frontend/src/editor/DuplicateGuard.tsx` renders inside both the Create New Record modal and the Inline Stub Creation modal, calling the duplicate-check endpoint with a 300 ms debounce and offering **Use existing** / **Create anyway**.

**Verify:** `tests/search/test_fusion.py` (RRF ordering, ties, semantic-only flag). Integration: with the embedder up, `autocomplete?q=round painting&types=Type` returns *tondo* flagged semantic-only; with the embedder stopped, the same request returns lexical hits and the degraded header. Playwright: duplicate guard shows a candidate when typing an existing Person's name.

**Commit:** `feat(search): hybrid RRF autocomplete and duplicate guard`

---

## Step 3.3 — Port `sparql_service` to pgvector (resolves the `/sparql/vector` question)

**Why:** Spec Open Question 1. LUX already has a tested SPARQL `SERVICE` endpoint; porting it is cheaper and safer than inventing a new contract.

**Do:**
1. Write `docs/decisions/0016-sparql-vector-service.md` (replacing the Proposed stub already there): decision = adopt the LUX `sparql_service` contract (`vec:similarTo`, `vec:score`, `vec:rank`, `vec:topK`, `vec:mode auto|constrained|global`, `vec:combine`, `vec:includeSeeds`) backed by `pgvector`; application-layer RRF (Step 3.2) remains the path for interactive autocomplete; the SERVICE is for SPARQL users and Tier 1 sweeps. List the known pitfalls below as operating constraints.
2. `backend/src/lae/sparql_service/parser.py`: vendor `lux/qlever-middletier/sparql_service/sparql.py` unchanged apart from imports (it parses QLever's `application/sparql-query` body, strips the appended `VALUES (?x) {…}` block, and rejects non-`vec:` triples with a 400).
3. `backend/src/lae/sparql_service/service.py`: port `VectorService` from `sparql_service/app.py`. Replace the LMDB/USearch store with: graph-first mode = `SELECT uri, 1 - (embedding <=> :seed) AS score FROM entity_embeddings WHERE uri = ANY(:candidates)`; vector-first mode = HNSW top-K. Keep the `head.vars` exact-match rule and `application/sparql-results+json` output.
4. Mount at `POST/GET /sparql/vector` in the `api` process. Enforce a 30 s internal timeout → HTTP 503 (QLever has no SERVICE timeout). Add `service-max-value-rows = 100000` to `deploy/Qleverfile` and document that above this QLever silently pushes nothing, so callers must use `vec:mode "constrained"` for large constraints.
5. The URL QLever calls must be the address *as QLever sees it* (`http://api:8000/sparql/vector` inside compose). Add `LAE_VECTOR_SERVICE_URL` to `Settings`.
6. Add a YASGUI snippet file `frontend/src/search/yasgui-snippets.ts` with one `SERVICE <…/sparql/vector>` example (used in Phase 4).

**Verify:** Vendored parser tests pass (`tests/sparql_service/`). From QLever: a query with `SERVICE <http://api:8000/sparql/vector> { ?c vec:similarTo "oil paint" ; vec:score ?s ; vec:topK 10 }` returns 10 rows; the same with a 5,000-IRI `VALUES` sibling returns scored rows only for those IRIs.

**Commit:** `feat(sparql): /sparql/vector SERVICE ported from LUX sparql_service onto pgvector`

---

## Step 3.4 — Agent identities, roles, audit log, quotas, circuit breaker

**Why:** Nothing agent-facing ships before governance exists.

**Do:**
1. Migration `0021_agent_governance.py`:
   - Roles `tier1_agent` (perms `edit_draft`, `propose_merge`) and `tier2_agent` (perms `review_proposal`, `approve_merge_scoped`) inserted into `roles`.
   - `agent_audit_log`: `id (bigint pk)`, `actor_id (UUID fk users)`, `on_behalf_of (UUID null)`, `session_id (UUID null)`, `tool (text)`, `arguments (JSONB)`, `result_summary (JSONB)`, `record_ids (UUID[])`, `provider (text null)`, `model (text null)`, `prompt_tokens (int)`, `completion_tokens (int)`, `cost_usd (numeric)`, `duration_ms (int)`, `status (enum: ok, error, denied)`, `error (text)`, `previous_id (bigint null)`, `created_at`. Append-only: no update/delete grants for the app role. `previous_id` links corrections (CHAI-TEA pattern).
   - `agent_quotas`: `actor_id (UUID pk)`, `calls_per_minute (int)`, `calls_per_day (int)`, `auto_merges_per_day (int)`, `tokens_per_day (bigint)`, `spend_usd_per_day (numeric)`, `updated_at`.
   - `system_settings` rows: `agents.paused (bool)`, `agents.auto_merge_paused (bool)`.
2. `backend/src/lae/agents/governance.py`: `check_quota(actor_id, kind) -> None | QuotaExceeded` (sliding counters from `agent_audit_log`), `circuit_open(kind: "all"|"auto_merge") -> bool`, `@audited(tool_name)` decorator that writes the audit row around any tool call (including on exception).
3. Service-account creation: `lae agents create --name "tier1-sweeper" --tier 1` mints a `users` row with `kind = agent` and an `api_tokens` row scoped to the tier's perms. Print the token once.
4. `GET/PUT /api/admin/agents/*`: list agents, quotas, audit log (paged, filterable by actor/tool/status), `POST /api/admin/agents/pause` and `/resume` (sets the `system_settings` flags, writes `admin_audit_log`).
5. Frontend `frontend/src/admin/AgentOpsConsole.tsx`: per-agent rate and spend, live tail of `agent_audit_log` (poll every 5 s), over-merge / unmerge counters per model (from `merge_batches` joined to the audit log), and a **Pause All Agents / Pause Auto-Merge** button with confirmation.

**Verify:** `tests/agents/test_governance.py`: quota exceeded returns `denied` audit rows; paused flag blocks every tool. Attempting `UPDATE agent_audit_log` as the app role fails.

**Commit:** `feat(agents): tiers, audit log, quotas, circuit breaker, ops console`

---

## Step 3.5 — MCP server at `/mcp` (resources and discovery tools)

**Why:** The read side first; agents must be able to see before they can act.

**Do:**
1. Add `mcp==2.x` (exact pin) to `pyproject.toml`. `backend/src/lae/mcp/server.py` creates a `FastMCP`-style server using the **stateless Streamable HTTP** transport and mounts it at `/mcp` in the `api` app. Auth: reuse `lae.auth` bearer-token validation; the authenticated `users` row becomes `ctx.actor`.
2. Resources in `backend/src/lae/mcp/resources.py`, each returning hydrated JSON:
   - `linkedart://record/{uuid}` — published record (labels transcluded).
   - `linkedart://draft/{uuid}` — projection of the live room via `RoomManager.snapshot(uuid)` plus the semantic diff vs. the published version.
   - `linkedart://profile/{class}`, `linkedart://shapes/{class}`, `linkedart://vocabularies`, `linkedart://vocabulary/{id}` — from `lae.profiles` and `lae.vocab` (mark cacheable; include `ETag`).
   - `linkedart://proposals/pending?type=merge`, `linkedart://proposal/{id}` (Step 3.8).
   - `linkedart://qa/worklists` (Step 3.10).
   - `linkedart://job/{id}` (Step 3.7).
3. Tools in `backend/src/lae/mcp/tools/discovery.py`, all wrapped with `@audited`:
   - `search_entities(query: str, classes: list[str] | None, vocab_id: str | None, limit: int = 20, mode: "hybrid"|"lexical" = "hybrid")` → calls the Step 3.2 endpoint logic directly.
   - `run_sparql_readonly(query: str, timeout_s: int = 20)` → sends to QLever through the Phase 2 read-only proxy (same method/keyword blocklist).
   - `get_backlinks(uri: str, limit: int = 50)` → Phase 2 backlinks service.
   - `reconcile_entity(record_uuid: str)` → Step 3.11.
4. Every tool returns `{"ok": bool, "data": …, "diagnostics": [{"code","path","params"}]}` so agents can self-correct.

**Verify:** `tests/mcp/test_resources.py` using the SDK's in-memory client: list resources, read `linkedart://record/{uuid}` for a corpus record, call `search_entities`. A token without `edit_draft` can still read; a token with no roles gets 403.

**Commit:** `feat(mcp): stateless /mcp server with resources and discovery tools`

---

## Step 3.6 — MCP draft-authoring and validation tools (through the room manager)

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: `RoomManager.transact` and `apply_change` are the only write path for agents; errors here let agents bypass validation, attribution, or undo.


**Why:** Agents edit the same live draft humans see, with a visible presence badge.

**Do:**
1. Vendor `linked-art/linkedart-parser/app/builder.py` into `backend/src/lae/builder/linkedart.py`. Keep the verified AAT constants, `_type`, `_name`, `_identifier`, `_statement`, `_iso`, `_timespan`, and the `Builder` reference helpers. **Delete the `Minter` class**: this system never mints label-derived URIs. Add unit tests that each helper output validates against the JSON Schema.
2. `backend/src/lae/mcp/tools/drafting.py`, all `@audited`, all requiring `edit_draft`, all performing edits via `RoomManager.transact(uuid, actor, fn)` which (a) opens or joins the room, (b) applies `fn(doc)` inside one Yjs transaction tagged with the actor, (c) sets awareness `{"kind": "agent", "name": actor.display_name}`, (d) returns the transaction id for turn-scoped undo:
   - `create_draft_record(type: str, preferred_name: str, language: str = "en")` → mints UUIDv7, creates the room, returns `uuid`.
   - `set_preferred_name(uuid, content, language, classification_uri = aat:300404670)`.
   - `add_statement(uuid, content, classification_uri, language)`.
   - `add_classification(uuid, type_uri, slot: str | None)` — `slot` must be a Value Set id from `lae.vocab`; the tool rejects URIs not in a `strict` set with diagnostic `vocab.not_allowed`.
   - `set_production(uuid, actor_uri: str | None, place_uri: str | None, timespan: {display, begin, end} | None, technique_uris: list[str] = [])`.
   - `add_equivalent(uuid, uri)`.
   - `set_place_wkt(uuid, wkt: str)` — validates with `shapely`, normalises winding (reuse the Phase 2 geometry helper).
   - `add_comment(uuid, text, anchor_node_id: str | None, mention_user_ids: list[str] = [])` → writes `comment_threads`/`comments`.
   Every referenced URI is checked against `entity_labels` or `records`; unknown URIs are rejected with `ref.unknown_target` (no `_label` fabrication).
3. `backend/src/lae/crdt/ops.py`: `apply_change(room, change: Change, actor) -> str` applies one typed semantic-diff `Change` (Phase 1 `lae.diff.semantic`: `add_node`, `remove_node`, `update_literal`, `add_ref`, `remove_ref`, `reorder`) to the live Y.Doc through `RoomManager.transact`, using the `lae.projection.ops` helpers. This is the single generic write path reused by the drafting tools above, by inverse-link authoring, smart paste, and the rebase modal in Phase 4. REST: `POST /api/records/{id}/draft/apply` body `{changes: [Change]}` (requires `edit_draft`) returning transaction ids.
4. `backend/src/lae/mcp/tools/validation.py`: `validate_draft(uuid)` → `lae.validation.validate(projection)` returning diagnostics grouped by severity; `preview_semantic_diff(uuid)` → the Phase 1 typed change objects.
5. Agent presence in the browser: `frontend/src/crdt/Presence.tsx` renders awareness entries with `kind == "agent"` using a distinct badge and the i18n key `editor:presence.agent`.

**Verify:** `tests/mcp/test_drafting.py`: an agent calls `create_draft_record` → `set_production` → `validate_draft`; a browser-equivalent `pycrdt` client connected to the same room receives the updates; the resulting projection round-trips through `lae.projection`. A Tier 1 token cannot call any Phase 1 publish endpoint (403).

**Commit:** `feat(mcp): draft-authoring and validation tools via RoomManager`

---

## Step 3.7 — `agent_jobs` asynchronous job model

**Why:** MCP Python SDK v2 removed the experimental Tasks API and does not implement the Tasks extension. Long-running work needs a server-owned job table.

**Do:**
1. Migration `0022_agent_jobs.py`: `agent_jobs`: `id (UUID pk, UUIDv7)`, `kind (text)`, `actor_id (UUID)`, `on_behalf_of (UUID null)`, `params (JSONB)`, `status (enum: queued, running, succeeded, failed, cancelled)`, `total (int)`, `done (int)`, `failed (int)`, `results (JSONB)` (per-item summaries), `error (text)`, `cancel_requested (bool default false)`, `priority (int default 5)`, `created_at`, `started_at`, `finished_at`.
2. `backend/src/lae/jobs/runner.py`: runs inside the `scheduler` process. Loop: claim with `FOR UPDATE SKIP LOCKED ORDER BY priority, created_at`, dispatch to a registered handler `JobHandler.run(job, progress: Callable, should_cancel: Callable)`. Handlers must check `should_cancel()` between items. Interactive-origin jobs (`on_behalf_of` set) get `priority = 1`.
3. Registry `backend/src/lae/jobs/registry.py` with `register("batch_enrich")`, `register("iiif_transcribe")`, and a placeholder `register("translate_record_fields")` that Phase 4 fills.
4. MCP: `start_job(kind, params)` returns `{job_id, resource: "linkedart://job/{id}"}`; `cancel_job(job_id)` sets `cancel_requested`; resource `linkedart://job/{id}` returns status and counters. Humans see the same via `GET /api/jobs`, `/api/jobs/{id}`, `POST /api/jobs/{id}/cancel`.
5. Frontend `frontend/src/admin/JobsPanel.tsx`: list, progress bar, cancel.

**Verify:** `tests/jobs/test_runner.py`: a 100-item fake handler reports progress, and `cancel_job` stops it within one item. Restarting the scheduler mid-run resumes `running` jobs from `done`.

**Commit:** `feat(jobs): agent_jobs runner, MCP job tools, jobs panel`

---

## Step 3.8 — Curation proposals queue and two-tier merge review

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: the proposal state machine and blast-radius guardrails decide which merges execute without a human; a logic error over-merges at scale.


**Why:** Cross-record merges never run from a single agent call; they pass through a queue with guardrails and human escalation.

**Do:**
1. Migration `0023_curation_proposals.py`: add `records.protected (bool not null default false)` with endpoints `POST /api/records/{id}/protect` and `POST /api/records/{id}/unprotect` (permission `protect`, both writing `admin_audit_log`) and a lock icon in the record header; publish of a protected record requires `protect` as well. Then `curation_proposals` exactly as in spec section 3.1: `id`, `proposal_type (enum: merge, split, bulk_reclassify)`, `source_record_id`, `target_record_id`, `status (enum: pending, claimed, approved, rejected, needs_human_review, executed, superseded)`, `proposed_by (UUID)`, `proposed_model (text)`, `evidence (JSONB)`, `field_resolution_plan (JSONB)`, `claimed_by (UUID null)`, `claim_expires_at`, `reviewed_by`, `review_rationale`, `block_resubmission (bool)`, `merge_batch_id (UUID null)`, `created_at`, `updated_at`. Unique partial index on `(least(source,target), greatest(source,target))` where `status in (pending, claimed, needs_human_review)`. Table `merge_blocklist`: `(record_a, record_b) pk` ordered, `reason`, `created_by`, `created_at`.
2. `backend/src/lae/curation/proposals.py`:
   - `propose_merge(actor, source_uri, target_uri, evidence, field_resolution_plan)`: reject if the pair is in `merge_blocklist` (`proposal.blocked`), if an open proposal exists (`proposal.duplicate`), or if either record is not `active`.
   - `claim_proposal(actor, id, lease_s = 600)`: sets `claimed`, `claimed_by`, `claim_expires_at`; a scheduler task returns expired claims to `pending`.
   - `guardrails_pass(source, target) -> GuardrailResult`: all of: neither record `protected`; QLever inbound backlink count of `source` < `system_settings.agents.auto_merge_backlink_threshold` **or** an identical external identifier (`equivalent` URI in ULAN / Wikidata / VIAF) on both; no contradictory core facts (different `type`; non-overlapping birth/death or begin/end when both present). Return the failing rule names.
   - `approve_merge(actor, id, rationale, final_field_plan)`: requires `approve_merge_scoped`; if `circuit_open("auto_merge")` or guardrails fail → `needs_human_review` with the rule names in `review_rationale`; else call `lae.curation.merge.execute_merge(plan_merge(source, target, final_field_plan), actor)` (Phase 2: active-room deferral, ordered locks, `merge_batches`) and set `executed` with `merge_batch_id`. Daily `auto_merges_per_day` quota applies.
   - `reject_merge(actor, id, rationale, block_future = True)`: `rejected`; inserts `merge_blocklist` when `block_future`.
   - `escalate_to_human(actor, id, rationale)`: `needs_human_review`; creates a `notifications` row for Editors watching either record.
3. MCP tools (`backend/src/lae/mcp/tools/proposals.py`): `propose_merge` (Tier 1), `claim_proposal`, `approve_merge`, `reject_merge`, `escalate_to_human` (Tier 2), each `@audited`.
4. REST for humans: `GET /api/proposals?status=needs_human_review`, `POST /api/proposals/{id}/approve|reject|unmerge` (Editor).
5. Frontend `frontend/src/curation/MergeWorkbench.tsx`: dual-pane field-by-field comparison (reuse the Phase 2 dual-pane layout), Tier 1 `evidence` and Tier 2 `review_rationale` panels, backlink counts, and **Approve / Reject (block future) / Unmerge** buttons. Keyboard-reachable; 24×24 px targets.

**Verify:** `tests/curation/test_proposals.py`: blocked pair rejected; guardrail failure routes to `needs_human_review`; approved merge creates `merge_batches` and `unmerge` restores both records; expired claims return to `pending`; paused auto-merge routes every approval to human review.

**Commit:** `feat(curation): proposal queue, guardrails, two-tier merge review, Merge Workbench`

---

## Step 3.9 — Tier 1 duplicate sweep (first agent)

**Why:** The queue needs a producer, and this proves the whole chain end to end.

**Do:**
1. `backend/src/lae/agents/sweeps/duplicates.py`: an `agent_jobs` handler `duplicate_sweep` that, for a `Set` or class + date window, runs `run_sparql_readonly` with the `/sparql/vector` SERVICE (Step 3.3) to find same-type pairs with cosine ≥ 0.92 or identical external identifiers, then asks the configured Tier 1 model (Step 3.12 adapter) to produce `evidence` and a `field_resolution_plan` as strict JSON, and calls `propose_merge`. The model never sees or emits URIs other than the two candidates.
2. Output schema for the model is a JSON Schema in `backend/src/lae/agents/schemas/merge_evidence.json`; validate locally with `jsonschema` before use (chai-engine does not validate replies locally; this system must).
3. Labelled test set `tests/corpus/duplicates/pairs.json` (≥50 true pairs, ≥50 false pairs, ask the domain lead). `scripts/eval_duplicate_sweep.py` prints precision/recall.

**Verify:** Precision ≥ 0.9 on the labelled set with the chosen Tier 1 model; every proposal has an `agent_audit_log` row with token counts.

**Commit:** `feat(agents): Tier 1 duplicate sweep job and evaluation script`

---

## Step 3.10 — QA Sweep, Link-Rot Checker, `qa_findings`, Queue Focus Mode

**Do:**
1. Migration `0024_qa.py`: `qa_findings`: `id (UUID pk)`, `code (text)`, `severity (enum: info, warning, error)`, `record_id (UUID)`, `node_id (text null)`, `params (JSONB)`, `status (enum: open, claimed, resolved, dismissed)`, `claimed_by`, `resolved_by`, `sweep_run_id (UUID)`, `created_at`, `updated_at`. `curation_campaigns`: `id`, `name (LangMap JSONB)`, `source (JSONB: {kind: set|smart_set|qa_code, ref})`, `assignee_ids (UUID[])`, `progress (JSONB: {done: [...], skipped: [...]})`, `created_by`, `created_at`.
2. `backend/src/lae/qa/sweeps.py`: scheduler tasks, each a SPARQL template in `backend/src/lae/qa/queries/*.rq`: `person.death_before_birth`, `object.produced_after_creator_death`, `hierarchy.part_of_cycle`, `hierarchy.broader_cycle`, `ref.unresolved_target` (from Phase 1 retained labels), `translation.stale_source` (placeholder for Phase 4), `sync.triple_mismatch` (sampled Postgres-vs-QLever audit from Phase 1). Upsert findings keyed by `(code, record_id, node_id)`; resolve findings that no longer reproduce.
3. `backend/src/lae/qa/linkrot.py`: `HEAD`/`GET` every `access_point` and IIIF `id` through the Phase 2 SSRF-filtered egress client, 10 concurrent, finding code `link.broken` with status code in `params`.
4. MCP resource `linkedart://qa/worklists` and tools `claim_finding(id)`, `resolve_finding(id, note)`.
5. Frontend `frontend/src/curation/QueueFocusMode.tsx`: full-screen sequential view over a campaign (`Prev / Next / Skip / Mark Reviewed`, progress count), opening each record in the editor pane. `POST /api/campaigns`, `GET /api/campaigns/{id}/next`.

**Verify:** Seed a `Person` with death before birth; the sweep creates one finding; fixing and publishing resolves it on the next run. Link-rot marks a 404 URL. Campaign over `qa_code = link.broken` walks the findings in order.

**Commit:** `feat(qa): sweeps, link-rot checker, qa_findings, curation campaigns, focus mode`

---

## Step 3.11 — On-demand reconciliation service `lae-reconcile`

**Why:** The LUX pipeline's reconciler is exact-key lookup over LMDB indexes with no HTTP wrapper; it must run as its own process with network off.

**Do:**
1. New process `backend/src/lae/reconcile/service.py` (`lae reconcile-service`), FastAPI on an internal port, loading the LUX LMDB indexes (`lux/data-pipeline/storage/idmap/lmdb.TabLmdb`, vendored) from `LAE_RECONCILE_INDEX_DIR`, with `allow_network = False`.
2. Vendor `extract_names` and the matching logic of `LmdbReconciler.reconcile` from `lux/data-pipeline/lux_pipeline/process/base/reconciler.py`, and the differents check. Rewrite the return type: `POST /reconcile` with a Linked Art record body returns `{"candidates": [{"uri", "source", "evidence": {"kind": "name"|"uri"|"identifier", "key": "<matched key>", "language": …}}], "blocked": [uri...]}`. Never mutate the input record.
3. `backend/src/lae/reconcile/client.py`: `ReconcileClient.reconcile(record) -> ReconcileResult` with a 10 s timeout and `ReconcileUnavailable` on failure.
4. MCP `reconcile_entity(record_uuid)` and REST `POST /api/records/{uuid}/reconcile` (Editor or Collaborator, explicit action only; never scheduled).
5. Frontend `frontend/src/editor/ReconcilePanel.tsx`: ranked candidates with evidence, **Add as equivalent** (writes through the room) or **Propose merge** (creates a `curation_proposals` row for local candidates).

**Verify:** With a small test index built from the corpus, a `Person` with a ULAN equivalent returns the matching local record with `evidence.kind == "uri"`; a blocked pair appears in `blocked`. The service refuses to start if `allow_network` is true.

**Commit:** `feat(reconcile): lae-reconcile service, client, MCP tool, panel`

---

## Step 3.12 — Multi-Provider LLM Adapter and encrypted provider configs

**Do:**
1. Migration `0025_llm_configs.py`: `llm_provider_configs`: `id (UUID pk)`, `name (text)`, `provider (enum: openai_compatible, gemini, anthropic, openai, vertex, bedrock, azure_openai)`, `base_url (text null)`, `model_id (text)`, `tier (enum: interactive, batch, arbiter)`, `is_local (bool)`, `api_key_ciphertext (bytea null)`, `key_wrapped_dek (bytea null)`, `owner_user_id (UUID null)` (null = institutional, set = BYOK), `enabled (bool)`, `pricing (JSONB null)`, `created_at`. `system_settings` rows: `llm.policy` (`local_only`|`allow_cloud`), `llm.byok_enabled (bool)`.
2. `backend/src/lae/llm/crypto.py`: envelope encryption: a per-row DEK (AES-256-GCM) wrapped by the instance master key from `LAE_MASTER_KEY` (or KMS later). Keys are decrypted only inside `lae.llm.adapter` and never logged or returned by any endpoint.
3. `backend/src/lae/llm/adapter.py`: `LLMAdapter.complete(config_id, messages, *, response_schema: dict | None, actor, on_behalf_of) -> Completion`. Copy (do not import) from `llm/chai-engine/chai/ai/llm.py` the functions that build LiteLLM kwargs, apply `response_format` with a strict JSON schema, and compute usage and cost; copy `extract_json` from `chai/ai/ai_utils.py` but make it **raise** on failure; copy `UsageLedger` from `chai/usage.py`. Validate every structured reply locally with `jsonschema`. Record every call via `@audited`.
4. Policy enforcement in the adapter: if `llm.policy == local_only`, refuse any config with `is_local == false`; BYOK configs are usable only when `owner_user_id == on_behalf_of`.
5. Admin REST `GET/POST/PUT/DELETE /api/admin/llm-configs` (System Admin; writes `admin_audit_log`), and `POST /api/me/llm-keys` for BYOK when enabled. Responses never include ciphertext or plaintext.
6. Frontend `frontend/src/admin/LlmConfigs.tsx` and `frontend/src/app/ModelSelector.tsx` (badges **Local** / **Cloud**, i18n keys `common:model.local`, `common:model.cloud`).

**Verify:** `tests/llm/test_crypto.py` (round-trip, wrong master key fails); `tests/llm/test_adapter.py` with LiteLLM `mock_response`: usage and cost recorded, invalid JSON raises, `local_only` blocks a cloud config, BYOK key of user A is unusable by user B. `grep -r api_key backend/src/lae/api` finds no logging of keys.

**Commit:** `feat(llm): multi-provider adapter, envelope-encrypted configs, BYOK, policy switch`

---

## Step 3.13 — Embedded Agent Chat (in-process MCP client, SSE, confirmation, turn undo)

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: permission intersection, the confirmation gate, and in-process MCP client wiring are the defence against prompt-based privilege escalation.


**Do:**
1. Migration `0026_chat.py`: `agent_chat_sessions`: `id`, `user_id`, `record_id (null)`, `set_id (null)`, `llm_config_id`, `title`, `created_at`, `last_activity_at`. `agent_chat_messages`: `id (bigint)`, `session_id`, `role (enum: user, assistant, tool)`, `content (JSONB)`, `tool_calls (JSONB null)`, `transaction_ids (text[])`, `created_at`.
2. `backend/src/lae/chat/orchestrator.py`: connects to the MCP server **in-process** through the SDK's in-memory client transport (no HTTP loopback), so every tool call inherits `@audited`, quotas, circuit breaker, and `RoomManager` routing. Tool list given to the model = MCP tools filtered by `effective_perms = user_perms ∩ tier_perms(llm_config.tier)`. Context chips are turned into the first user message (record JSON-LD + draft diff, workbench pair, selected rows, selected graph nodes, or a `qa_findings` item).
3. Confirmation gate: tools tagged `high_impact` (`approve_merge`, `start_job` with more than one record, `propose_merge`) are not executed; the orchestrator emits an SSE `confirm_required` event with the arguments and waits for `POST /api/chat/{session}/confirm/{call_id}`.
4. Turn-scoped undo: each draft-editing turn collects the `RoomManager.transact` ids; `POST /api/chat/{session}/turns/{n}/revert` calls `RoomManager.revert(uuid, transaction_ids)` (Yjs `UndoManager` scoped to the agent's origin).
5. `POST /api/chat/{session}/messages` streams `text/event-stream` events: `token`, `tool_call`, `tool_result`, `confirm_required`, `done`. Attribution on later publish: `record_revisions.authors` includes both the user and the agent service user (`AI Agent (via <user>)` rendering is a frontend concern).
6. Frontend `frontend/src/chat/ChatDrawer.tsx`, `ContextChips.tsx`, `ToolCard.tsx`, `ConfirmCard.tsx`, `RevertTurnButton.tsx`; `@agent` in an anchored comment thread opens the drawer with the thread's node as a chip. `aria-live="polite"` announces only `done`.

**Verify:** `tests/chat/test_orchestrator.py` with a mocked LLM: a Collaborator asking to publish gets no `publish` tool in the list; `approve_merge` yields `confirm_required` and executes only after confirm; revert removes the turn's edits from the room and nothing else. Playwright: drawer streams tokens, tool card links to the draft diff.

**Commit:** `feat(chat): embedded agent chat orchestrator with SSE, confirmation, turn undo`

---

## Exit Checklist

- [ ] `entity_embeddings` populated for every eligible `entity_labels` row; `lae.api` image contains no torch.
- [ ] Hybrid autocomplete returns semantic-only hits with the badge; lexical fallback works with the embedder stopped.
- [ ] `/sparql/vector` answers QLever `SERVICE` queries with and without pushed-down `VALUES`; decision record `docs/decisions/0016-sparql-vector-service.md` is marked Accepted.
- [ ] Tier 1 and Tier 2 service accounts exist; every MCP tool call produces an `agent_audit_log` row; Pause All Agents blocks every tool within one request.
- [ ] An agent can create, edit, validate, and comment on a draft through `/mcp` and a human sees the edits live with an agent badge; the agent cannot publish.
- [ ] `agent_jobs` runs, reports progress, cancels, and resumes after a scheduler restart.
- [ ] Duplicate sweep precision ≥ 0.9 on the labelled pair set; every auto-merge is unmergeable in one click; guardrail and circuit-breaker routing verified.
- [ ] QA sweeps and link-rot produce and resolve `qa_findings`; Queue Focus Mode walks a campaign.
- [ ] `lae-reconcile` returns candidates with evidence and never reaches the network.
- [ ] LLM configs are encrypted at rest, keys never appear in responses or logs, `local_only` and BYOK rules are tested.
- [ ] Chat drawer: permission intersection, confirmation cards, and Revert Agent Turn verified by tests.

## Common Mistakes

- Writing a tool that updates `records` or `crdt_updates` directly instead of going through `RoomManager`. Humans will not see the edit and `draft_epoch` will drift.
- Letting the model return a URI it was not given. Every URI in a tool argument must come from `search_entities`, `reconcile_entity`, the duplicate-check endpoint, or a context chip.
- `import chai` or `import lux_pipeline`. Vendor the functions; the packages drag in torch, `.env` loading, and the whole LUX `Config`.
- Using `litellm>=…`. Pin the exact version with the comment about the compromised 1.107.0 release.
- Running the reconciliation service with network access, or scheduling it. It is on-demand only.
- Treating `approve_merge` as a merge. It is a request; guardrails, the circuit breaker, and quotas decide whether the merge executes or goes to a human.
- Logging prompts or tool arguments that contain API keys or BYOK material. The audit row stores arguments; strip `api_key`-like fields before writing.
- Forgetting `service-max-value-rows` in the Qleverfile and then concluding the vector SERVICE "returns nothing" for large constraints.
