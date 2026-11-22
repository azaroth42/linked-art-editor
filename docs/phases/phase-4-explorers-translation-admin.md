# Phase 4 — Explorers, Translation & Full Operations: Step-by-Step Guide

**Read first:** `docs/README.md`, `docs/conventions.md`, `docs/glossary.md`, `docs/reuse_map.md`, `docs/spec/` (section numbers are preserved from the original design document) sections 3.4, 4.2, 6.5, 6.6, 6.7, 6.9, 10.3–10.5, 12, and the Phase 1–3 guides' Exit Checklists (everything there is assumed to exist).
**Goal:** Round out the macro-explorers, localization and data translation, the structural workflows (split, reclassify, inverse links, smart paste, offline rebase), the complete Administration Control Plane, and hardening.
**You are done when** every item in the Exit Checklist at the bottom passes.

Rules for this phase:
- Nothing here may change the publish transaction, the sync worker's diff logic, or the projection rules. Explorers and workflows consume the Phase 1–3 APIs; if an API is missing, add a new endpoint, do not alter an existing contract.
- Every explorer is an **alternate view of QLever results**. The Grid, Map, Graph, and Timeline all accept the same `ResultSet` type (Step 4.1). Do not build per-explorer query code.
- Every LLM call goes through `lae.llm` (Phase 3). No direct provider SDK imports anywhere in Phase 4.
- Every translation written into a record is **additive** and carries `AttributeAssignment` provenance. Overwriting source text is a bug.
- Every drag-and-drop or canvas interaction gets a keyboard equivalent in the same PR.
- Migrations in this phase are numbered `0030`–`0039`.

---

## Step 4.1 — Shared `ResultSet` contract and explorer routing

**Why:** Grid, Map, Graph, Timeline, Smart Sets, and YASGUI all need to hand each other the same thing. Define it once.

**Do:**
1. Backend: `backend/src/lae/search/resultset.py` with a Pydantic model `ResultSet`:
   `source` (enum `sparql | smart_set | set | qa_worklist | campaign | adhoc`), `source_ref` (text; SPARQL string, set UUID, or smart set UUID), `columns[]` (`name`, `kind: uri | literal | number | date | wkt`), `rows[]` (list of dicts), `total` (int or null), `cursor` (text or null), `generated_at`.
2. Endpoint `POST /api/search/resultset` accepting `{source, source_ref, limit, cursor}` and returning a `ResultSet`. For `sparql` it runs the query read-only through the Phase 1 `lae.sync.qlever_client.QLeverClient` with the public timeout; for `set` it runs the member query; for `smart_set` it compiles the saved query (Step 4.6).
3. Frontend: `frontend/src/search/resultset.ts` exporting the `ResultSet` type and `useResultSet(source, ref)` (TanStack Query, keyed by source+ref+cursor).
4. `frontend/src/search/OpenInMenu.tsx`: a menu with **Grid**, **Map**, **Graph**, **Timeline**, **Save as Smart Set**. It takes a `ResultSet` and navigates to `/explore/{view}?source=…&ref=…`. Mount it on the Search Workbench, the Grid, the YASGUI console, and every Set page.
5. Route `frontend/src/app/routes/explore.tsx` that reads `view`, `source`, `ref` from the URL and renders the matching explorer component.

**Verify:** `uv run pytest tests/search/test_resultset.py` (sparql, set, smart_set sources return identical column shapes); Playwright: open a Set, choose Open In → Grid, then Grid → Map; the URL carries source and ref and the same rows appear.

**Commit:** `feat(search): shared ResultSet contract and Open In routing`

---

## Step 4.2 — Force-Directed Graph Explorer

**Why:** Spec 6.6. Multi-record WebGL graph with supernode protection, drag-to-form, cluster-to-merge, path finder, and live draft overlay.

**Do:**
1. Backend `backend/src/lae/graph/neighborhood.py`:
   - `GET /api/graph/neighbors?uri=…&predicates=…&limit=25` → `{nodes[], edges[], truncated: {predicate: remaining_count}}`. Nodes: `uri`, `type`, `label` (hydrated via `entity_labels`), `degree`. Edges: `source`, `target`, `predicate`.
   - Counting per predicate uses one `SELECT (COUNT(?o) AS ?n) WHERE { <uri> <pred> ?o }` per candidate predicate, exactly the pattern in `lux/qlever-middletier/services/hal.py`, cached in a new table `graph_degree_cache` (`uri text`, `predicate text`, `count int`, `computed_at`, pk `(uri, predicate)`), invalidated by the sync worker when `uri` is a subject of an applied outbox event. Migration `0030_graph_degree_cache.py`.
   - Expansion returns at most `limit` neighbours per predicate and reports the remainder in `truncated` so the client renders a cluster badge ("+ 14,200 more HumanMadeObjects").
   - `GET /api/graph/path?from=…&to=…&max_hops=4` → shortest path using a QLever property-path query per hop count (1..4), returning the first non-empty result.
2. Frontend `frontend/src/explorers/graph/`:
   - `GraphExplorer.tsx` using `sigma` + `graphology` (`@react-sigma/core`). Node colour by Linked Art class (one palette entry per class in `graph/palette.ts`); edge style by predicate.
   - `useGraphStore.ts` (zustand): nodes, edges, selected set, expansion history, overlay mode.
   - `expandNode(uri)` calls the neighbours endpoint; cluster badge nodes (`kind: "cluster"`) expand into the next page when clicked.
   - **Drag-to-form:** a node drag starts an HTML5 drag with `application/x-linked-art-ref+json` (same payload as the Phase 2 Entity Workbench) so nodes can be dropped on form slots.
   - **Cluster-to-merge:** lasso select (`sigma` custom drag handler) → "Open in Merge Workbench" with the first two selected records; more than two shows a warning.
   - **Path finder:** two-node selection → "Find path" → highlights the returned path.
   - **Live draft overlay:** if a selected node's record has an open draft in the current user's rooms, request `POST /api/records/{id}/draft/diff` (Phase 1 semantic diff) and render `add_ref` edges dashed green and `remove_ref` edges dashed red.
   - **Accessibility:** `GraphNodeInspector.tsx` lists the current node, its edges, and neighbour buttons as a semantic HTML list; arrow keys move between neighbours; `prefers-reduced-motion` pauses the ForceAtlas2 layout and uses a static circular layout.
3. Performance: bench `GraphExplorer` with the synthetic fixture `frontend/src/explorers/graph/__fixtures__/5000.json` (5,000 nodes, 12,000 edges); must hold 60 fps idle and under 100 ms per expansion render on a laptop.

**Verify:** `pytest tests/graph/` (degree cache hit/miss, truncation counts, path 1–4 hops); Playwright: expand a supernode and see a cluster badge; keyboard-only walk through `GraphNodeInspector`; axe passes.

**Commit:** `feat(graph): force-directed explorer with supernode protection, path finder, draft overlay`

---

## Step 4.3 — Neighborhood mini-graph (React Flow)

**Do:**
1. `frontend/src/editor/panels/MiniGraph.tsx` using `@xyflow/react`, rendering the open record ± 1–2 hops from the same `/api/graph/neighbors` endpoint, with dagre layout (`@dagrejs/dagre`) for `part_of`, `member_of`, and carrier/work chains.
2. Mount it as a collapsible drawer under the Backlinks panel. Clicking a node opens the record in the second pane of the dual-pane editor (Phase 2).
3. Accessible fallback: the Backlinks list already exists; the drawer's toggle announces "Mini graph shown/hidden" via the existing polite live region.

**Verify:** Component test renders 1-hop graph from a fixture; axe passes.

**Commit:** `feat(editor): React Flow neighborhood mini-graph drawer`

---

## Step 4.4 — Multi-Record Map Explorer

**Do:**
1. Backend `GET /api/explore/places?source=…&ref=…` → for every row in the `ResultSet` that is a `Place` or has `took_place_at` / `current_location` / `encountered_at`, return `{uri, label, wkt, via_predicate}`. Implement in `backend/src/lae/explorers/places.py` as one QLever query joining the result URIs (as `VALUES`) to their place geometries.
2. `POST /api/explore/places/within` with a bounding box (`minx, miny, maxx, maxy`) and a source/ref → filters via GeoSPARQL `geof:sfWithin` on QLever (see `lux/qlever-middletier/README.md` for the `qlss:` spatial-search example if the plain function is slow).
3. Frontend `frontend/src/explorers/map/MapExplorer.tsx` with MapLibre GL JS, clustered markers (`supercluster`), Terra Draw rectangle tool for bounding-box filtering, and a results side list.
4. Accessible fallback: the side list is the primary DOM; markers are `aria-hidden`; the bounding-box tool has a numeric `minx/miny/maxx/maxy` form.

**Verify:** `pytest tests/explorers/test_places.py`; Playwright draws a box and the side list shrinks; axe passes.

**Commit:** `feat(map): multi-record map explorer with GeoSPARQL bounding-box filter`

---

## Step 4.5 — Chronological Timeline (single-record and cross-record)

**Do:**
1. Backend `backend/src/lae/explorers/timeline.py`:
   - `GET /api/records/{id}/timeline` → events from the record's own JSON-LD: `produced_by`, `changed_ownership_through[]` (provenance activities), `used_for`, `encountered_by`, `created_by`, each with `begin`, `end`, `label`, `path`, and `anomaly` flags (`reversed_bounds`, `overlaps_previous_custody`, `gap_over_years`).
   - `GET /api/explore/timeline?source=…&ref=…` → one row per record with its principal `TimeSpan`, plus creator lifespans when `carried_out_by` is a Person, flagging `after_creator_death`.
2. Frontend `frontend/src/explorers/timeline/`:
   - `RecordTimeline.tsx` (strip under the provenance section in the editor; clicking a marker scrolls to the form card by `path`).
   - `CrossRecordTimeline.tsx` (zoomable, `d3-scale` + SVG; accepts a `ResultSet`).
   - Both render an equivalent `<table>` toggle for screen readers with the same rows.
3. Anomalies feed the Phase 3 QA sweeper: add rule `timeline_after_creator_death` to `lae.qa.rules` so the cross-record view and `qa_findings` agree.

**Verify:** Unit tests for anomaly detection on corpus provenance examples; Playwright toggles table view.

**Commit:** `feat(timeline): record provenance strip and cross-record timeline`

---

## Step 4.6 — Visual Query Builder and Dynamic Smart Sets

**Do:**
1. Backend:
   - Migration `0031_smart_sets.py`: create `saved_smart_sets` with `id (UUID pk)`, `name (JSONB LangMap)`, `definition JSONB` (the builder AST), `compiled_sparql TEXT`, `compiler_version INT`, `owner_id`, `shared BOOL`, `last_count INT`, `last_counted_at`, `created_at`, `updated_at`.
   - `backend/src/lae/search/querybuilder.py`: `compile(ast) -> str`. AST nodes: `{class}`, `{path, op, value}` where `op ∈ eq | contains | missing | present | before | after | within_bbox`, `and`, `or`, `not`. Paths are UI Profile slot paths (so catalogers never see predicates); the compiler maps them to Linked Art predicate chains via `lae.profiles` and emits QLever SPARQL using `lae.sparql.builder` (vendored from `lux/qlever-middletier/qleverlux/sparql/`).
   - Endpoints: `POST /api/smart-sets` (create from AST), `GET /api/smart-sets/{id}` , `POST /api/smart-sets/{id}/run` (returns a `ResultSet` with `source: smart_set`), `GET /api/smart-sets/{id}/sparql` (shows compiled query).
2. Frontend `frontend/src/search/querybuilder/QueryBuilder.tsx`: class picker, facet rows bound to profile slots and Value Sets, multi-hop ("made of" → concept picker, "produced in" → place picker), a live count, and **Save as Smart Set**. Smart Sets appear in the sidebar and open in the Grid by default.
3. Smart Sets are first-class `source` values in Step 4.1 so Map/Graph/Timeline and Curation Campaigns (Phase 3) accept them.

**Verify:** `pytest tests/search/test_querybuilder.py` with golden SPARQL for 10 ASTs; the spec example ("all HumanMadeObjects made of oil paint in Amsterdam missing a Dimension") compiles and returns rows on the dev dataset.

**Commit:** `feat(search): visual query builder compiling to SPARQL, dynamic smart sets`

---

## Step 4.7 — Embedded YASGUI console

**Do:**
1. `frontend/src/search/yasgui/SparqlConsole.tsx` embedding `@zazuko/yasgui` (or `@triply/yasgui`) pointed at the public read-only `/sparql` proxy (Phase 2).
2. Autocomplete: register a YASQE autocompleter fed by `GET /api/sparql/completions` returning Linked Art prefixes, classes, predicates, and AAT terms from `entity_labels` (top 5,000 by usage).
3. Snippets menu: Linked Art prefixes, backlinks, and the `/sparql/vector` SERVICE snippet (Phase 3 contract).
4. Results toolbar gets the Step 4.1 **Open In** menu: the current query becomes `source: sparql`.
5. Hard rules: Update keywords (`INSERT`, `DELETE`, `LOAD`, `CLEAR`, `DROP`) are rejected client-side with a localized message; the proxy already rejects them server-side.

**Verify:** Playwright: run `SELECT * { ?s a <…Person> } LIMIT 5`, Open In → Grid shows 5 rows; attempt `INSERT DATA` shows the rejection message.

**Commit:** `feat(search): embedded YASGUI console wired to Open In`

---

## Step 4.8 — Two-speed LLM routing and the interactive-first semaphore

**Why:** Track A, Track B, and Public Reader translation all share the Phase 3 `lae.llm` adapter but need routing and fairness.

**Do:**
1. `backend/src/lae/llm/routing.py`: `ModelProfile` (`id`, `provider_config_id`, `tier: interactive | batch`, `max_input_tokens`, `languages[]`). Settings `system_settings.llm_routing = {interactive: profile_id, batch: profile_id}` editable in the admin policy screen (Step 4.16).
2. `backend/src/lae/llm/queue.py`: an `asyncio` priority semaphore: `acquire(priority: int)`; priorities `0` interactive human, `1` Public Reader ephemeral, `2` MCP/batch jobs. Concurrency limits per tier from `system_settings.llm_concurrency`. Export Prometheus gauges `lae_llm_queue_depth{tier,priority}`.
3. `backend/src/lae/llm/glossary.py`: `build_glossary(record_jsonld, target_lang) -> list[(source_term, target_term)]` by collecting every referenced `entity_labels` row with a `labels[target_lang]` plus Value Set labels from `lae.vocab`, and rendering a prompt block. This is **new code**: `chai-engine`'s `GlossaryTranslator` (`chai/translator.py`) is a regex term swap, not prompt injection, and must not be copied for this purpose.
4. `backend/src/lae/llm/translate.py`: `translate_text(text, source_lang, target_lang, glossary, priority) -> TranslationResult` with fields `text`, `model_id`, `model_version`, `prompt_hash`, `usage`, `cost`. Store the rendered prompt hash (chai-engine never stored the rendered prompt; this is required for provenance).

**Verify:** `pytest tests/llm/test_routing.py` (interactive request acquires before queued batch requests under contention); `test_glossary.py` builds terms from a corpus record with French labels.

**Commit:** `feat(llm): two-speed routing, priority semaphore, graph-grounded glossary`

---

## Step 4.9 — Track A: UI and config localization pipeline

**Do:**
1. `backend/src/lae/cli_i18n.py` registered as `la-i18n` (`[project.scripts]`): sub-commands `extract` (scan `frontend/public/locales/en/*.json` and `profiles/**/*.yaml` LangMaps), `translate --lang fr`, `validate --lang fr`, `status`.
2. Lockfile `i18n/lock.json`: per key `{ "hash": sha256(en text), "status": "machine_draft" | "human_reviewed", "model": …, "translated_at": … }`. `translate` only drafts keys whose hash is missing or changed and never touches `human_reviewed` entries.
3. ICU AST validator `backend/src/lae/i18n/icu.py`: parse source and target with `icu-messageformat-parser` port (or shell out to `@formatjs/icu-messageformat-parser` via a tiny Node script in `scripts/icu-validate.mjs`); assert same placeholder set, valid plural categories for the target language (CLDR), identical tag set, and length ≤ 1.6× source. On failure, retry once with the error text appended to the prompt, then mark `needs_human`.
4. First locale pack: run `la-i18n translate --lang fr` with the batch tier, commit `frontend/public/locales/fr/*.json` and the lockfile, and add `fr` to the language switcher.
5. Vocabulary Manager: button **Translate Missing Labels & Scope Notes** calling `POST /api/vocab/translate-missing?lang=fr`, which first resolves labels from sidecar datasets (`entity_labels.labels`) and only then calls `translate_text`; results are stored with `auto_translated: true` in the `LangMap` metadata and badged until a Vocabulary Admin confirms.

**Verify:** `la-i18n validate --lang fr` exits 0; mutate an `en` key and `la-i18n status` lists it as stale; CI job `i18n` runs `validate` for every locale directory.

**Commit:** `i18n: la-i18n pipeline with ICU validation, lockfile, and first fr locale pack`

---

## Step 4.10 — Track B: additive, provenance-tracked data translation

**Do:**
1. `backend/src/lae/translation/data.py`:
   - Allowlist: only `Name.content` and `LinguisticObject.content`. Paths outside the allowlist raise `TranslationNotAllowed`.
   - `translate_node(room, node_iri, target_lang, actor)`: reads the node from the live `RoomManager` draft, calls `translate_text`, and **appends** a sibling node in the same keyed Y.Map: for `Name`, `classified_as` gets AAT `300417194` (Translated Title) plus the original classifications; `language` set to the target; `content` as `Text`; `assigned_by: [AttributeAssignment]` with `carried_out_by` the agent service account, `timestamp`, `classified_as` translation activity, and a `source_content_hash` identifier (sha256 of the source text) in `identified_by`; `_order` placed after the source node.
   - Record `model_id` and `prompt_hash` in the `AttributeAssignment` as `Identifier`s classified with local Type records minted in `profiles/vocab/translation.yaml`.
2. Frontend: `frontend/src/widgets/langtext/TranslateToMenu.tsx` on every multilingual field; streams via SSE from `POST /api/records/{id}/draft/translate` and shows a **Machine-translated** badge (`identified_by` hash present and no human revision).
3. Staleness: the Phase 3 QA sweeper gets rule `translation_stale`: for every node with `source_content_hash`, recompute from the current sibling source content; mismatch → `qa_findings` row with `kind: translation_stale` and a 1-click **Re-translate** action that calls `translate_node` again.
4. MCP tool `translate_record_fields(record_ids[], target_lang, paths[])` in `lae.mcp.tools.translation`, enqueued on `agent_jobs` (Phase 3 runner) at priority 2; progress at `linkedart://job/{id}`; each record edit goes through the room manager so humans see it live.
5. Publish path: when a draft containing machine translations is published, `entity_labels.labels[target_lang]` and `entity_embeddings` update in the existing publish transaction (no new code path; verify the existing label extractor picks up the new `Name`).

**Verify:** `pytest tests/translation/` (allowlist rejection; sibling node shape; hash; staleness detection); Playwright: translate a title, see the badge, edit the source, run the sweeper, see the finding.

**Commit:** `feat(translation): additive Name/LinguisticObject translation with AttributeAssignment provenance and staleness QA`

---

## Step 4.11 — Ephemeral Public Reader translation

**Do:**
1. Migration `0032_ephemeral_translations.py`: table `ephemeral_translations` (`record_id UUID`, `version INT`, `target_lang TEXT`, `model_id TEXT`, `path TEXT`, `text TEXT`, `created_at`, pk `(record_id, version, target_lang, model_id, path)`).
2. `POST /api/public/{id}/translate?lang=…` (anonymous, rate-limited per IP via the Phase 2 limiter): on cache miss, translate the primary description statement(s) at priority 1 and store; new revisions change `version`, so old rows are dead and are purged by the scheduler weekly.
3. Public Reader template: a **Translate description (Local AI)** button shown only when `allow_public_translation` is on in `system_settings`; result rendered in a clearly labelled box ("Automated translation, not part of the record").
4. Admin: **Flush ephemeral translations** button in the policy screen.

**Verify:** Two requests for the same record/lang hit the LLM once (assert on `lae_llm_requests_total`); publishing a new revision invalidates.

**Commit:** `feat(reader): ephemeral on-demand translation cache`

---

## Step 4.12 — Entity Split Workbench and Unsplit

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: split reassigns inbound references across many records with ordered locks and must be exactly reversible.


**Do:**
1. Migration `0033_split_batches.py`: `split_batches` (`id UUID pk`, `source_record_id`, `new_record_id`, `manifest JSONB` [original record, reassigned backlinks as `{referrer_id, path, old_target, new_target}`], `executed_by`, `executed_at`, `reverted_at NULL`). `record_revisions.split_batch_id` already exists from Phase 0.
2. `backend/src/lae/curation/split.py`:
   - `plan_split(source_id, partition: {keep_paths[], move_paths[], equivalent_split: {keep[], move[]}}, backlink_moves: [{referrer_id, path}])` → a dry-run manifest.
   - `execute_split(plan, actor)`: refuse if the source has an active room; `SELECT … FOR UPDATE` source and every referrer in UUID order; mint a new UUIDv7 record with the moved statements; rewrite the chosen inbound references in each referrer; write one `record_revisions` row per touched record sharing `split_batch_id`; enqueue `outbox_events` for all; commit.
   - `unsplit(batch_id, actor)`: reverse from the manifest in one transaction, same locking.
   - Endpoints `POST /api/records/{id}/split/plan`, `POST /api/records/{id}/split/execute`, `POST /api/split-batches/{id}/unsplit`.
3. Frontend `frontend/src/curation/split/SplitWorkbench.tsx`: reuse the Phase 3 dual-pane layout; left = source, right = new record; statements are moved with drag-and-drop **and** a "Move →" button per statement; backlinks panel per side with checkboxes for reassignment; final diff preview before execute.
4. `curation_proposals` of type `split` (Phase 3 queue) open directly in this workbench.

**Verify:** `pytest tests/curation/test_split.py`: split, assert backlink rewrite, unsplit restores byte-identical records and revisions; concurrent room check blocks.

**Commit:** `feat(curation): entity split workbench with reversible split_batches`

---

## Step 4.13 — Safe Record Reclassification Wizard

**Do:**
1. `backend/src/lae/curation/reclassify.py`:
   - `GET /api/records/{id}/reclassify/check?target_type=Group` → `{compatible_paths[], incompatible_paths[], inbound: [{predicate, count, compatible: bool}]}` using the UI Profile of the target class for outbound fields and a predicate-range table `profiles/shapes/predicate_ranges.yaml` for inbound predicates (e.g. `took_place_at` requires `Place`).
   - `POST /api/records/{id}/reclassify` → opens or reuses the draft room, sets `type`, moves compatible fields, parks incompatible subgraphs under a draft-only key `_unresolved` (stripped on publish and blocking publish via a `sh:Violation` until empty).
2. Frontend `frontend/src/curation/reclassify/ReclassifyWizard.tsx`: steps Target class → Compatibility report → Resolve incompatibles (each gets Delete / Move to path / Keep as other) → Open draft.

**Verify:** Tests for `Person`→`Group` (compatible), `Place`→`Group` with inbound `took_place_at` (flagged), publish blocked while `_unresolved` is non-empty.

**Commit:** `feat(curation): record reclassification wizard with inbound compatibility check`

---

## Step 4.14 — Inverse-Link Authoring, Smart Paste, composite creation

**Do:**
1. **Inverse-Link Authoring:** in the Backlinks panel add **Add Linked Record** per predicate group. It opens a picker, then calls `POST /api/records/{referrer_id}/draft/apply` with a typed change (`add_ref` at the inverse path) through the room manager, so the *other* record's draft is mutated and appears in the Review Inbox. Backend helper `lae.crdt.ops.apply_change(room, change, actor)` exists from Phase 3 Step 3.6; reuse it.
2. **Smart Paste (URLs/WKT):** route pasted text in any slot through the Phase 2 Resource Introspection Proxy when it is a URL or WKT; insert the generated subgraph via `apply_change`.
3. **Smart Paste (prose):** `backend/src/lae/parse/`:
   - Copy `linked-art/linkedart-parser/app/schemas.py` as `lae/parse/schemas.py` (extraction schemas and `FIELD_KINDS`) and `app/builder.py` as `lae/parse/builder.py`; strip the `Minter` (unmatched terms become inline draft stubs instead of minted URIs) and make AAT constants resolve through `lae.vocab`.
   - Pipeline as in `app/pipeline.py`: (1) extract strings with `lae.llm` using the schema as `response_format`; (2) candidates from Phase 3 hybrid search over local records and sidecars (never live Getty/Wikidata); (3) LLM selects per term or `none`; (4) builder assembles; (5) result is applied to a **new draft room** via `apply_change`, never published.
   - `POST /api/parse/prose` with `{entity_type, text}`; frontend `frontend/src/editor/SmartPasteProse.tsx` in the Create Record dialog with a review step listing every selection and its alternatives.
4. **Composite creation flows:** `frontend/src/editor/create/CompositeCreate.tsx` with templates `Object + VisualItem`, `Object + DigitalObject`, `Person + Group membership`; each mints linked draft stubs and relies on the Phase 1 bundled publish.

**Verify:** Prose paste of the spec "Night Café" example produces a draft `HumanMadeObject` with linked creator candidate and no minted external URIs; inverse-link creates a draft on the referrer; axe passes on all dialogs.

**Commit:** `feat(editor): inverse-link authoring, smart paste (URL/WKT/prose), composite creation`

---

## Step 4.15 — Rebase Offline Changes Modal (v1)

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: diffing a stale offline Y.Doc against a newer revision and applying selected changes safely is the hardest client-server interaction in the system.


**Do:**
1. Backend: when `crdt-ws` rejects a reconnect for a stale `draft_epoch`, it returns `{code: STALE_EPOCH, current_epoch, base_version}`; the client then calls `POST /api/records/{id}/draft/offline-diff` with its stale Y.Doc update, and the server projects it, diffs against the current published revision using the Phase 1 semantic diff, and returns typed changes (`add_ref`, `remove_ref`, `update_literal`, `add_node`, `remove_node`).
2. Frontend `frontend/src/crdt/RebaseModal.tsx`: a list of the user's offline changes with **Apply** / **Discard** per row and **Apply all compatible**; Apply calls `apply_change` into the fresh room. No automatic three-way merge. Changes whose target node no longer exists are shown disabled with a reason.
3. Discarded changes are offered as a downloadable JSON patch.

**Verify:** `pytest tests/crdt/test_offline_rebase.py`; Playwright: go offline, edit, publish from another session, reconnect, apply one change, discard one.

**Commit:** `feat(crdt): v1 rebase offline changes modal`

---

## Step 4.16 — Administration Control Plane: policy, users, tokens, audit

**Do:**
1. Migration `0034_admin.py`: alter the Phase 2 `admin_audit_log` (created in migration 0016) to add `ip (inet null)`, `prev_hash (text)`, and `row_hash (text)` for tamper evidence (`row_hash = sha256(prev_hash || canonical_json(row))`, backfilled in order for existing rows); create `idp_group_roles` (`idp_group text pk`, `role_id`); create `usage_metrics_daily` (`day date`, `dimension text`, `key text`, `metric text`, `value numeric`, pk `(day, dimension, key, metric)`).
2. Every admin mutation goes through `lae.admin.audit.record(actor, action, target, before, after)`; add a test that greps the admin routers for mutations without an audit call.
3. **Policy screen** `frontend/src/admin/Policy.tsx` editing `system_settings` with versioning (`system_settings.version`, previous values kept): draft TTL, rebuild threshold, SPARQL timeouts and rate limits, Tier 2 guardrails and daily caps, `local_only | allow_cloud`, BYOK toggle, chat retention, LLM routing (Step 4.8), `allow_public_translation`, and the **SSRF allowlist/denylist** used by the introspection proxy, link-rot checker, and webhooks (`lae.net.egress.is_allowed(url)` must read it).
4. **Users & roles** `frontend/src/admin/Users.tsx`: directory of humans and agent accounts; IdP group → role mapping table `idp_group_roles`; suspend (revokes OIDC sessions via `lae.auth.sessions.revoke_all(user_id)` and disconnects rooms via `RoomManager.eject(user_id)`); **View Effective Permissions** showing `user_perms ∩ agent_tier_perms` for a chosen agent tier.
5. **Tokens** `frontend/src/admin/Tokens.tsx`: issue/rotate/revoke PATs and client credentials with scopes, expiry, last-used, last IP.
6. **Usage analytics** `frontend/src/admin/Analytics.tsx`: scheduler job `rollup_usage_daily` fills `usage_metrics_daily` from `record_revisions`, `agent_audit_log`, request logs, and LLM usage; per-user, per-class, per-function dashboards with date range and CSV/JSON export at `GET /api/admin/analytics/export`.

**Verify:** `pytest tests/admin/` (audit chain verifies; policy version rollback; suspension ejects a live room; analytics rollup idempotent); axe passes on all admin routes.

**Commit:** `feat(admin): policy, users/roles/tokens, tamper-evident audit log, usage analytics`

---

## Step 4.17 — Outbound signed webhooks and bulk LOUD exports

**Do:**
1. Migration `0035_webhooks.py`: `webhooks` (`id`, `url`, `secret_encrypted`, `events text[]` ⊂ {publish, merge, split, delete, escalation}, `active`, `created_by`); `webhook_deliveries` (`id`, `webhook_id`, `outbox_event_id`, `status`, `attempts`, `response_code`, `last_error`, `next_retry_at`).
2. `backend/src/lae/webhooks/dispatcher.py` running inside `sync-worker`: after an outbox event is applied, fan out to matching webhooks; body = ActivityStreams item identical to `/activity/stream`; headers `X-LAE-Signature: sha256=<hmac>` and `X-LAE-Delivery`; exponential retry (1m, 5m, 30m, 2h, 12h) then dead-letter; URL checked by `lae.net.egress.is_allowed`.
3. Admin `frontend/src/admin/Webhooks.tsx`: CRUD, test delivery, delivery log with **Retry**.
4. Bulk LOUD export: scheduler job `bulk_export` and `lae export --format jsonld|nt --out …` writing gzip-compressed JSON-LD lines and N-Triples with internal `/node/` IRIs stripped (reuse `lae.projection.strip_internal_iris` and the Phase 1 N-Triples path); admin screen lists exports with download links; `export_runs` table in migration `0036_exports.py`.

**Verify:** `pytest tests/webhooks/` (signature verifies with the stored secret; denylisted URL refused; retry schedule); an export of the dev dataset re-imports via the Phase 2 batch CLI without diff.

**Commit:** `feat(ops): HMAC-signed outbound webhooks and scheduled bulk LOUD exports`

---

## Step 4.18 — Embedding model migration, protected records, batch rollback, restore verification

**Do:**
1. **Embedding migration:** migration `0037_embedding_shadow.py` adds `entity_embeddings.embedding_next halfvec` (dimension configurable) and `embedding_model_next`; `lae embeddings migrate --model …` re-embeds in batches into the shadow column with progress in `agent_jobs`; `lae embeddings swap` renames columns and rebuilds the HNSW index inside one transaction; the vector SERVICE reads the active column name from `system_settings.embedding_model`.
2. **Protected records console** `frontend/src/admin/Protected.tsx`: list, bulk lock/unlock (`records.protected` from Phase 3), with audit (`records.protected` from Phase 3 Step 3.8).
3. **Batch rollback console** `frontend/src/admin/Batches.tsx`: unified list of Batch Staging commits (Phase 2), `merge_batches` (Phase 2/3), and `split_batches` (Step 4.12) with **Rollback / Unmerge / Unsplit**, plus cleanup actions for stuck `curation_proposals` leases and stale `qa_findings`.
4. **Restore verification:** scheduler job `verify_backup` (weekly): restore the latest `pgBackRest` backup into a scratch database (`lae_verify`), run `alembic current`, count `records` and `record_revisions`, compare checksums of a sampled 1,000 records against production, write the result to `backup_jobs` (`verified_at`, `verification_ok`, `details JSONB`), and alert on failure through the health dashboard.

**Verify:** `pytest tests/admin/test_embedding_swap.py` (queries return identical top-k before and after a no-op swap); restore verification runs green in a staging environment and writes a `backup_jobs` row.

**Commit:** `feat(ops): embedding model migration, protected/batch consoles, restore verification`

---

## Step 4.19 — Hardening: accessibility audit, explorer performance, chaos tests

**Do:**
1. **WCAG 2.2 AA audit:** extend the Playwright + axe suite to every route (reader, editor, every explorer, every admin screen) and add manual-check records in `docs/a11y/audit-phase4.md` for SC 2.5.7 (drag alternatives), 2.5.8 (24×24 targets: add a Playwright check that measures every interactive element's bounding box), 2.4.11/2.4.13 (focus not obscured with sticky headers and drawers open), and `aria-live` discipline (assert no live-region updates during remote CRDT keystrokes).
2. **Explorer performance:** `frontend/perf/` Playwright traces for Graph (5,000 nodes), Map (50,000 markers clustered), Grid (10,000 rows virtualized), Timeline (2,000 events); thresholds recorded in `docs/perf/phase4.md` and enforced as CI warnings.
3. **Chaos tests** `backend/tests/chaos/`: kill `sync-worker` mid-batch and assert replay from `qlever_index_watermark` yields the same QLever state; restart QLever and assert persisted updates survive; kill `crdt-ws` during edits and assert rooms rehydrate from `crdt_updates` with no lost updates; drop the LLM service and assert translation features degrade with a visible status and the core loop is unaffected.
4. Disaster-recovery drill: follow `docs/runbooks/dr.md` (write it if missing) end to end in staging and record timings.

**Verify:** All chaos tests pass in CI nightly; the a11y audit document has no open blockers; perf thresholds met.

**Commit:** `test: phase 4 hardening (a11y audit, explorer perf, chaos tests)`

---

## Exit Checklist

- [ ] Grid, Map, Graph, Timeline, YASGUI, and Smart Sets all exchange the same `ResultSet` via **Open In**, and each has a keyboard/table equivalent that passes axe.
- [ ] Graph Explorer holds 60 fps on the 5,000-node fixture and never expands more than `limit` neighbours per predicate without a cluster badge.
- [ ] `la-i18n validate` passes for every locale; `fr` locale pack committed; Vocabulary Manager translate-missing resolves sidecar labels before calling the LLM.
- [ ] Data translation only ever appends sibling nodes with `AttributeAssignment` and `source_content_hash`; the staleness sweeper and Re-translate work end to end.
- [ ] Ephemeral Public Reader translation is cached per revision and never touches `records` or QLever.
- [ ] Split and Unsplit, Reclassify, Inverse-Link, Smart Paste (URL/WKT/prose), composite creation, and the Rebase modal are covered by tests and Playwright flows.
- [ ] Admin: policy versioning, SSRF allowlist enforced by proxy/link-rot/webhooks, users/roles/tokens with session ejection, tamper-evident `admin_audit_log` verified by a chain check, usage analytics export, webhooks with signature and retries, bulk LOUD exports that re-import cleanly, embedding swap, protected/batch consoles, restore verification job green.
- [ ] Chaos tests pass nightly; DR drill timings recorded; a11y audit has no open blockers.
- [ ] Every spec feature is either shipped or listed in `docs/deferred.md` with a reason and an owner.

## Common Mistakes

- Building a second query path for an explorer instead of consuming `ResultSet`.
- Expanding a supernode fully "just for the demo" and shipping it; the per-predicate `limit` and cluster badge are mandatory.
- Calling a provider SDK directly from translation code instead of `lae.llm`, which bypasses routing, the semaphore, quotas, and the circuit breaker.
- Copying `chai-engine`'s `GlossaryTranslator` for glossary injection; it rewrites strings after the fact and is not what the design requires.
- Translating `Identifier` content, `TimeSpan` bounds, or WKT. The allowlist is `Name.content` and `LinguisticObject.content` only.
- Writing a translation into the source node, or into `records` directly, instead of appending a sibling in the live room.
- Reusing the parser's `Minter` so unmatched terms get fake external URIs; unmatched terms must become inline draft stubs.
- Executing a split or reclassification while the record has an active room.
- Attempting an automatic three-way merge in the Rebase modal. v1 is a per-change Apply/Discard list.
- Adding an admin mutation without an `admin_audit_log` entry, or letting a webhook URL bypass the SSRF egress check.
- Swapping the embedding column without rebuilding the HNSW index in the same transaction.
