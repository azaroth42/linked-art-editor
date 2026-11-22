# Phase 2 — Curation, Vocabularies & Scale: Step-by-Step Guide

**Read first:** `docs/README.md`, `docs/conventions.md`, `docs/glossary.md`, `docs/spec/` (section numbers are preserved from the original design document) sections 3.4, 4.3, 5, 6.1–6.6, 7, 10, 12, `docs/reuse_map.md`, and the Phase 1 guide (`docs/phases/phase-1-core-loop.md`) so you know the names it created.
**Goal:** The editor becomes the daily tool for a cataloging team working against the full record volume: controlled vocabularies, backlinks, merge/unmerge, faceted search, drag-and-drop, sidecar authorities, batch import, change feed, and the operational safety nets (blue/green rebuilds, health, backups, maintenance mode).
**You are done when** every item in the Exit Checklist at the bottom passes.

Rules for this phase:
- Phase 1 names are fixed. Do not rename `lae.projection`, `lae.rdf`, `lae.crdt.rooms.RoomManager`, `lae.sync.worker`, `lae.validation.validate`, `ProfileRenderer`, `useRecordDoc`, `EntityRefAutocomplete`, or any existing REST path. Add; do not reshape.
- Migrations in this phase are numbered `0010`–`0019`. Never edit a migration that has been merged.
- Every new REST endpoint gets a permission check from `lae.auth` using the permission names in spec section 8.2 (`edit_draft`, `publish`, `delete`, `merge`, `unmerge`, `protect`, `manage_vocabularies`, `manage_system`, …). An endpoint without a permission check is a bug.
- Every new UI surface gets a keyboard path and an `axe` check in the e2e suite before its step is "done".
- Do not build anything from Phase 3 or 4 (no embeddings, no MCP, no LLM, no force graph, no split/reclassify). If a step seems to need one of them, it does not; re-read the spec.
- Steps are ordered by dependency. Do them in order.

---

## Step 2.1 — Vocabulary registry tables and loader (migration 0010)

**Why:** Pickers, `classified_as` card partitioning, validation enforcement, and drag-and-drop targeting all read from one registry. It must exist before anything else in this phase.

**Do:**
1. Migration `0010_vocabularies.py` creating:
   - `vocabularies`: `id (text pk, e.g. "vocab:work_title_types")`, `label (JSONB LangMap)`, `scope_note (JSONB LangMap null)`, `mode (enum: enumerated, rule)`, `rule (JSONB null)` with keys `metatype_uris: text[]`, `broader_roots: text[]`, `exclusions: text[]`, `classes: text[]`, `enforcement (enum: strict, extensible, advisory)`, `version (int)`, `updated_at`, `updated_by (UUID)`.
   - `vocabulary_terms`: `vocabulary_id (text fk)`, `uri (text)`, `position (int)`, `is_default (bool)`, `deprecated (bool)`, `scope_note (JSONB LangMap null)`, `label_override (JSONB LangMap null)`; pk `(vocabulary_id, uri)`.
   - `slot_vocabulary_bindings`: `id (bigint pk)`, `entity_class (text)`, `property_path (text)`, `vocabulary_id (text fk)`, `enforcement (enum, null = inherit)`; unique `(entity_class, property_path)`.
   - `vocabulary_membership`: `vocabulary_id (text fk)`, `uri (text)`, `refreshed_at`; pk `(vocabulary_id, uri)`. This is the materialized membership cache for `rule` mode.
   - Add a GIN index on `entity_labels.vocab_ids` and on `entity_labels.metatype_uris`.
   - Add `entity_labels.usage_count (int not null default 0)`, refreshed by a nightly scheduler job `refresh_usage_counts` running `SELECT ?o (COUNT(?s) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?o` over the default graph in QLever (chunked by type) and updating in one `UPDATE … FROM`. Autocomplete and term pickers order by it.
2. `lae/vocab/loader.py`: `load_from_yaml(path: Path) -> None` reads every file in `profiles/vocab/` (schema from Phase 0 Step 0.12) and upserts `vocabularies`, `vocabulary_terms`, `slot_vocabulary_bindings`. `export_to_yaml(path: Path) -> None` does the inverse, deterministic key order. Both are CLI sub-commands: `lae vocab import`, `lae vocab export`.
3. `lae/vocab/membership.py`: `refresh_membership(vocabulary_id: str) -> int`. For `enumerated` it copies `vocabulary_terms`. For `rule` it runs a QLever query over the default graph plus `urn:graph:sidecar:*` graphs: concepts whose `crm:P2_has_type` includes any `metatype_uris`, or that are reachable via `skos:broader+` / `crm:P127_has_broader_term+` from `broader_roots`, minus `exclusions`, filtered by `classes`. Build the query with the vendored SPARQL builder (Step 2.2 item 1). Write results into `vocabulary_membership` in one transaction, then update `entity_labels.vocab_ids` for affected URIs with one `UPDATE … FROM`.
4. `lae/vocab/service.py`: `get_bundle() -> VocabBundle` returning every `enumerated` vocabulary with resolved labels (from `entity_labels.labels`, overridden by `label_override`) plus all bindings; cache in-process keyed by `max(vocabularies.updated_at)`; compute an ETag from that timestamp.
5. Endpoints (router `lae/api/vocab.py`):
   - `GET /api/vocab/bundle` → bundle JSON with `ETag`; returns `304` on `If-None-Match`. Permission: any authenticated user.
   - `GET /api/vocab/{id}/terms?q=&limit=` → for `rule` vocabularies, prefix search over `vocabulary_membership` joined to `entity_labels` ordered by `usage_count desc, default_label`. Permission: any authenticated user.
6. Seed `profiles/vocab/` with: `person_name_parts`, `work_title_types`, `identifier_types`, `statement_types`, `dimension_types`, `measurement_units` (enumerated); `gender`, `occupation`, `nationality`, `type_of_work`, `style`, `material` (rule, via AAT metatype URIs from LUX `globals.json`: nationality 300379842, occupation 300263369, style 300015646, gender 300055147). Bind each to `(class, path)` per spec section 7.2.

**Verify:**
```
uv run lae vocab import && uv run lae vocab export /tmp/vocab-out && diff -r profiles/vocab /tmp/vocab-out
uv run pytest backend/tests/vocab -q
curl -i localhost:8000/api/vocab/bundle | grep -i etag
```
`diff` prints nothing; tests cover import/export round-trip, membership refresh for one rule vocabulary against a test QLever index, and the ETag 304 path.

**Commit:** `db: vocabulary registry tables (0010)` then `feat(vocab): YAML loader, membership refresh, bundle endpoint`

---

## Step 2.2 — Vendor the SPARQL builder and wire vocab into validation

**Why:** Every QLever query from here on is built by code, not string formatting. Validation must enforce Value Sets before the pickers ship, or catalogers will save values the next deploy rejects.

**Do:**
1. `lae/sparql/terms.py` was vendored in Phase 1 (Step 1.3). Now copy `lux/qlever-middletier/qleverlux/sparql/builder.py` (and its tests) into `lae/sparql/builder.py`, `backend/tests/sparql/`. Keep `iri()` and `string_literal()` exactly; add `InsertData` and `DeleteData` classes (delegating to the Phase 1 `lae/sparql/updates.py` string builders) (the LUX `TODO.md` section 5.1 describes the missing pieces). Replace any `luxql` import with nothing; the builder must have no LUX dependency. Note in `lae/sparql/README.md` the known limits (multi-variable `OrderBy`, `Values` quoting of strings starting with `http`).
2. `lae/validation/vocab_rules.py`: `compile_bindings() -> list[Rule]`. For each binding, produce a rule that checks every value at `property_path` on records of `entity_class`: in `vocabulary_membership` (or `vocabulary_terms` for enumerated) → ok; not present and `enforcement = strict` → `Diagnostic(code="vocab.not_in_value_set", severity="violation")`; `extensible` → `severity="warning"`; `advisory` → `severity="info"`. Params: `{vocabulary_id, uri, path}`.
3. Register the rules in `lae.validation.validate` after JSON-Schema and before SHACL. Rules recompile when `max(vocabularies.updated_at)` changes (reuse the bundle cache key).
4. Also emit the equivalent `sh:in`-style constraints into `profiles/shapes/_generated_vocab.ttl` on `lae vocab import` so MCP and RDF consumers see the same policy (Phase 3 reads this; generate it now).

**Verify:** `uv run pytest backend/tests/validation/test_vocab_rules.py -q`: a `Person` with `classified_as` outside `vocab:gender` fails with `violation` when strict, `warning` when extensible. Publishing such a draft through `POST /api/records/{id}/publish` returns `422` with the diagnostic code.

**Commit:** `feat(sparql): vendored injection-safe builder with INSERT/DELETE DATA` then `feat(validation): Value Set enforcement rules`

---

## Step 2.3 — Vocabulary-aware pickers and classification cards

**Why:** This is what catalogers see from the registry.

**Do:**
1. `frontend/src/vocab/useVocabBundle.ts`: TanStack Query hook fetching `/api/vocab/bundle` with ETag caching; exposes `getVocabulary(id)`, `getBindings(cls)`, `routeClassification(cls, uri, entityLabel)` which returns the slot id whose vocabulary contains `uri` (check `vocab_ids` on the hydrated label, then enumerated term lists) or `null`.
2. `frontend/src/widgets/ValueSetPicker.tsx`: for `enumerated` → segmented pills (≤6 terms) or select; for `rule` → combobox calling `GET /api/vocab/{id}/terms?q=` with 150 ms debounce, pinned shortlist shown on focus. Every option shows the term's scope note on hover and via `aria-describedby`. Deprecated terms are hidden for new values but rendered (with a "deprecated" badge) when already present.
3. `frontend/src/editor/ClassificationCards.tsx`: reads `classified_as` from the Y.Doc via `useRecordDoc`, partitions by `routeClassification`, renders one card per bound slot plus an **Other Classifications** card for unmatched URIs (removable chips, never dropped on save). Wire into `ProfileRenderer` for the `classification` widget type.
4. Keyboard: all pickers operate with arrow keys, `Enter`, `Escape`; chips are removable with `Delete` when focused. Target size ≥ 24×24 CSS px.
5. e2e: `frontend/e2e/vocab-pickers.spec.ts` — add a gender via keyboard only, add an unbound classification via the raw JSON-LD inspector and assert it appears under Other Classifications; `axe` passes.

**Verify:** `pnpm test && pnpm test:e2e --grep vocab` green.

**Commit:** `feat(editor): Value Set pickers and classification cards`

---

## Step 2.4 — Vocabulary Manager admin UI

**Do:**
1. Endpoints in `lae/api/vocab_admin.py`, all requiring `manage_vocabularies`:
   - `PUT /api/vocab/{id}` (create/update definition), `DELETE /api/vocab/{id}` (refused with `409` if any binding references it),
   - `PUT /api/vocab/{id}/terms` (full ordered list), `POST /api/vocab/{id}/refresh` (runs `refresh_membership`, returns count),
   - `PUT /api/vocab/bindings` (full list), `GET /api/vocab/{id}/usage` → `{uri, count}[]` from QLever (`SELECT ?uri (COUNT(?s) AS ?n) WHERE { ?s ?p ?uri } GROUP BY ?uri` with `VALUES ?uri { … }` over the terms).
   - Every mutation writes `admin_audit_log` (table created in Step 2.17; until then, write to a Python logger and add the table write in that step).
2. `frontend/src/admin/vocab/VocabManagerPage.tsx` (route `/admin/vocab`): left nav by Linked Art class → bindings; main pane shows the bound vocabulary: mode toggle, enforcement select, term list with `@dnd-kit` reorder **and** Move Up / Move Down buttons, LangMap pill switcher for label and scope note, deprecate toggle showing usage count, **Replace Across Records** button (enabled in Step 2.8).
3. "Import YAML" / "Export YAML" buttons call `POST /api/vocab/import` (multipart) and `GET /api/vocab/export` (zip).

**Verify:** e2e `frontend/e2e/vocab-admin.spec.ts`: reorder terms with keyboard, change enforcement to strict, confirm the bundle ETag changes and a new draft picker reflects it without reload (bundle query refetches on window focus).

**Commit:** `feat(vocab-admin): Vocabulary Manager UI and admin endpoints`

---

## Step 2.5 — Backlinks and delete protection

**Why:** Delete, merge, and the Vocabulary usage counts all need inbound references.

**Do:**
1. `lae/sparql/backlinks.py`: `get_backlinks(uri: str, limit_per_predicate: int = 25) -> BacklinkSummary` running one grouped query: `SELECT ?p (COUNT(?s) AS ?n) WHERE { ?s ?p <uri> } GROUP BY ?p`, then one query per predicate for the first `limit_per_predicate` subjects with their `rdf:type`. Resolve labels via `entity_labels`. Internal `/node/` subjects are mapped to their parent record (strip after `/node/`) and deduplicated.
2. `GET /api/records/{id}/backlinks` → `{predicates: [{predicate, count, items: [{uri, type, label}]}], total}`. Permission: any authenticated user.
3. `DELETE /api/records/{id}`: requires `delete`; if `total > 0` return `409 {code: "record.referenced", total}`; otherwise set `status = deleted`, write an `outbox_events` row with `kind = delete`, and a `record_revisions` row with summary `deleted`. Public `/id/{uuid}` returns `410 Gone` for deleted records (all content types).
4. `lae/sync/worker.py`: handle `kind = delete` by issuing `DELETE DATA` for every triple in the last revision's N-Triples (the owned-subject set).
5. `frontend/src/editor/BacklinksPanel.tsx`: collapsible panel grouped by predicate with counts, each item a link; "Unlink" action opens the referencing record's draft in a new tab at the referencing slot (deep link `?focus=<path>` handled by `ProfileRenderer`). Delete button in the record header shows the `409` reason with the panel expanded.

**Verify:** `backend/tests/api/test_delete.py`: deleting a referenced record returns `409`; after unlinking and republishing the referrer, delete succeeds and QLever no longer returns the subject's triples; `/id/{uuid}` returns `410`.

**Commit:** `feat(records): backlinks endpoint, delete protection, tombstones`

---

## Step 2.6 — Manual merge and unmerge (migration 0011)

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: merge and unmerge rewrite other records inside one transaction with ordered locks; a mistake corrupts referrers and cannot be undone by the manifest.


**Why:** Agents will propose merges in Phase 3; the human merge must be correct and reversible first.

**Do:**
1. Migration `0011_merge_batches.py`: `merge_batches` with `id (UUID pk)`, `source_record_id`, `target_record_id`, `executed_by`, `executed_at`, `manifest (JSONB)` holding `source_record` (full JSON-LD), `target_record_before`, `referrers: [{record_id, version_before, json_paths: []}]`, `reverted_at (null)`.
2. `lae/curation/merge.py` (module path is fixed; Phase 3 imports `lae.curation.merge`):
   - `plan_merge(source_id, target_id, field_plan: dict | None) -> MergePlan`: load both, compute inbound referrers from `get_backlinks` (all, not limited), and the merged document using per-class rules copied from `lux/data-pipeline/lux_pipeline/process/merger.py` (`RecordMerger.merge_common` and the per-class functions; strip their `configs`/`idmap` arguments and pass plain dicts). First value wins for scalars; lists are deduplicated by URI/content. `field_plan` overrides per top-level property (`"source" | "target" | "both"`).
   - `execute_merge(plan, actor) -> merge_batch_id`: refuse if either record has a `crdt_drafts` row or an open room (`RoomManager.is_open`, declared in Phase 1) → `409 {code: "merge.active_draft"}`. In one transaction: `SELECT … FOR UPDATE` both `records` rows ordered by UUID; re-check both `status = active`; write the manifest; rewrite every referrer's JSON (replace source URI with target URI at the recorded paths) and bump each referrer's version with a `record_revisions` row (`merge_batch_id` set, summary `merge:referrer`); write the merged target as a new version; append source URI to target `equivalent`; set source `status = merged`, `merged_into = target_id`; write `outbox_events` (`kind = merge`, one row for the target plus one `publish` row per referrer, one `delete` row for the source).
   - `unmerge(merge_batch_id, actor)`: refuse if `reverted_at` is set; restore every referrer and the target from the manifest as new forward versions; set source back to `active`; write outbox rows; set `reverted_at`.
3. Public `/id/{uuid}` for `status = merged` returns `301` to the target URI.
4. Endpoints: `POST /api/records/{id}/merge-into/{target_id}` (`merge`; body `{field_plan}`; dry-run with `?plan=1` returns the `MergePlan` without executing), `POST /api/merge-batches/{id}/unmerge` (`unmerge`), `GET /api/merge-batches?record_id=`.
5. `frontend/src/merge/MergeWorkbench.tsx` (route `/merge/{sourceId}/{targetId}`): dual pane via a shared `DualPane.tsx` layout, field-by-field comparison with `source | target | both` radios per property, "Preview merged record" (uses `?plan=1`), Execute button with confirm dialog, and an Unmerge button on the target record header when a non-reverted batch exists.

**Verify:** `backend/tests/merge/test_merge_unmerge.py`: merge A→B with two referrers; assert referrers' JSON points at B, A is `301`, QLever has no triples with subject A and referrers link to B; unmerge restores all four records byte-for-byte to their prior JSON and QLever matches.

**Commit:** `db: merge_batches (0011)` then `feat(merge): manual merge with manifest and 1-click unmerge`

---

## Step 2.7 — Faceted search workbench

**Do:**
1. `lae/search/facets.py`: `search(q, filters, page) -> SearchResult`. Text match via the QLever text index: `?s ql:has-word "w"` per token (copy the per-word pattern from `lux/qlever-middletier/qleverlux/query/text.py`; do not copy the `<scope>Words` SERVICE views, they are LUX index-time artifacts), prefix via `ql:contains-word "w*"` when the token is the last one. Filters: `class`, `classified_as`, `made_of`, `carried_out_by`, `date range` (on `begin_of_the_begin` / `end_of_the_end`), `has_iiif` (exists `representation`), `language`. Facet counts computed with a `SELECT DISTINCT ?s` subquery then `GROUP BY ?facet` (pattern from `translator.py:458-498`). Draft/QA status facet comes from Postgres (`crdt_drafts` join) and is applied after the QLever page.
2. `GET /api/search?q=&class=&facet.<name>=&page=` → results with hydrated labels, plus `facets`. Cache with the LRU pattern from `qleverlux/clients/qlever.py` **only for anonymous requests**; authenticated requests bypass the cache.
3. `frontend/src/search/SearchPage.tsx` (route `/search`): query box, facet rail (checkbox groups with counts), result list, "Open in Grid" button (Step 2.8). This is the default landing route after login.
4. `frontend/src/app/CommandPalette.tsx`: `Cmd+K` / `Ctrl+K` palette querying `/api/search/autocomplete` for records and a static action list (New record, Search, Inbox, Vocab Manager, Admin). Fully keyboard operable.

**Verify:** e2e `search.spec.ts`: search a known label, filter by class, open the record; `axe` passes. Backend test asserts facet counts equal direct COUNT queries on the test index.

**Commit:** `feat(search): faceted search workbench and command palette`

---

## Step 2.8 — Grid view and batch find-and-replace (migration 0012)

**Do:**
1. Migration `0012_batches.py`: `edit_batches` with `id (UUID pk)`, `kind (enum: find_replace, import, vocab_migrate)`, `created_by`, `created_at`, `status (enum: staged, committed, reverted)`, `manifest (JSONB)` listing `{record_id, version_before}` plus the operation spec, `committed_at`, `reverted_at`.
2. `lae/batch/service.py`: `stage_find_replace(selector, path, old_uri, new_uri) -> batch_id` resolves the selector (a `Set` id, a saved search, or an explicit id list) via QLever to record ids, stores the manifest; `commit(batch_id)` applies the replacement per record in chunks of 200 within transactions, each creating a new version with `edit_batches.id` in `record_revisions.summary` and an outbox row; refuses records with active drafts and lists them in the response; `revert(batch_id)` restores `version_before` for every record as forward versions.
3. Endpoints: `POST /api/batches/find-replace` (`publish`), `POST /api/batches/{id}/commit`, `POST /api/batches/{id}/revert`, `GET /api/batches/{id}`.
4. Hook the Vocabulary Manager's **Replace Across Records** to `stage_find_replace` with `kind = vocab_migrate`.
5. `frontend/src/grid/GridView.tsx` (route `/grid?set=` or `/grid?search=`): TanStack Table whose columns come from the UI Profile slots of the dominant class (`ProfileRenderer` already exposes `slotsFor(cls)`); cells are read-only until the row is "opened", which attaches `useRecordDoc` for that record and renders the slot widget inline. Fill-down (copy a cell to selected rows) and multi-row find-and-replace stage an `edit_batches` row and show a review dialog before commit. Batch publish calls `/api/records/{id}/publish` per row and reports per-row diagnostics.

**Verify:** backend test: find-replace over 500 fixture records commits in chunks, creates 500 revisions, QLever reflects the new URI, revert restores; e2e: fill-down a `classified_as` value across three rows and publish.

**Commit:** `db: edit_batches (0012)` then `feat(grid): grid view, fill-down, batch find-and-replace`

---

## Step 2.9 — Comments, watchlists, notifications, Review Inbox (migration 0013)

**Do:**
1. Migration `0013_collab.py`:
   - `comment_threads`: `id (UUID pk)`, `record_id`, `anchor_iri (text)` (record URI or internal node IRI), `status (enum: open, resolved)`, `created_by`, `created_at`, `resolved_by`, `resolved_at`.
   - `comments`: `id (UUID pk)`, `thread_id`, `author_id`, `body (text)`, `mentions (UUID[])`, `created_at`.
   - `watchlists`: `user_id`, `target_kind (enum: record, set, vocabulary)`, `target_id (text)`, `created_at`; pk `(user_id, target_kind, target_id)`.
   - `notifications`: `id (bigint pk)`, `user_id`, `event_type (text)`, `payload (JSONB)`, `read_at`, `created_at`. Payloads are typed (`{record_id, actor_id, version}` etc.), never prose.
2. `lae/collab/`: `threads.py` (CRUD, resolve), `watch.py` (toggle, list), `notify.py` with `emit(event_type, payload, recipients)`; call `emit` from publish, merge, delete, comment-with-mention, and `crdt_drafts` first-edit (via `RoomManager` hook).
3. Endpoints: `GET/POST /api/records/{id}/threads`, `POST /api/threads/{id}/comments`, `POST /api/threads/{id}/resolve`, `PUT/DELETE /api/watch/{kind}/{id}`, `GET /api/notifications?unread=1`, `POST /api/notifications/{id}/read`, extend the Phase 1 `GET /api/drafts` with `?contributor=&validation=&expires_within=` (Editor-only; joins `crdt_drafts` with a cached validation summary refreshed on each room save). Extend TTL and Discard reuse the Phase 1 endpoints `POST /api/records/{id}/draft/extend` and `POST /api/records/{id}/draft/discard`.
4. Frontend: `editor/CommentThreads.tsx` (anchored to slot ids via a `data-anchor-iri` attribute `ProfileRenderer` already renders), `app/NotificationBell.tsx`, `inbox/ReviewInbox.tsx` (route `/inbox`, Editor-only) with filters and the three actions Extend TTL / Inspect & Publish / Discard. "Watch" toggle in the record header.
5. Scheduler: `lae/scheduler/reaper.py` already exists from Phase 1; add a daily `expiring_soon` notification for watchers 24 h before expiry.

**Verify:** backend tests for thread lifecycle, mention notification, inbox filters; e2e: leave a comment on a Dimension, resolve it, see the notification.

**Commit:** `db: comments, watchlists, notifications (0013)` then `feat(collab): threads, watchlists, notification inbox, review inbox`

---

## Step 2.10 — Record cloning and starter templates

**Do:**
1. `POST /api/records/{id}/clone` (`edit_draft`): loads the published JSON-LD, mints a new UUIDv7 and base URI, re-mints every internal `/node/` IRI, removes `identified_by` entries whose `classified_as` is in `vocab:identifier_types` marked `unique` (add a boolean `unique` to `vocabulary_terms.scope_note`'s sibling: new column `vocabulary_terms.flags (JSONB)`), hydrates a draft room, returns the new id.
2. `profiles/templates/<class>-<slug>.json`: starter records; `GET /api/templates?class=`, `POST /api/records/from-template/{name}`.
3. Frontend: "Create similar" in the record header; "New record" dialog lists templates per class. Composite flow "Object + VisualItem" is a template pair whose second record references the first; both publish via the existing bundled publish.

**Verify:** test that a clone has no shared `/node/` IRIs with its source and no accession number.

**Commit:** `feat(records): clone and starter templates`

---

## Step 2.11 — Entity Workbench drag-and-drop with keyboard equivalents

**Do:**
1. `frontend/src/workbench/EntityWorkbench.tsx`: collapsible side panel with three tabs (Search, Recent, Pinned). Pinned list persists in `localStorage` keyed by user id. Items are draggable with `@dnd-kit` and carry `application/x-linked-art-ref+json` `{id, type, _label, classified_as}` plus `text/uri-list` for native HTML5 drags.
2. `frontend/src/editor/dropTargets.ts`: `validTargets(payload, cls, profile, bundle) -> Set<slotId>` using slot `widget === "entity_ref"`, the slot's allowed classes from the UI Profile, and `routeClassification` for concept payloads. `ProfileRenderer` highlights valid slots during a drag (`data-drop-valid`), dims invalid ones, and auto-expands collapsed sections after 400 ms hover.
3. Drop writes into the Y.Map keyed by target URI (idempotent). Keyboard equivalent: every workbench item has a "Link to slot…" menu listing valid slots; selecting one performs the same write. Announce via the polite live region.
4. e2e: keyboard-only linking of a pinned Person into `produced_by.carried_out_by`; `axe` passes.

**Verify:** `pnpm test:e2e --grep workbench` green.

**Commit:** `feat(editor): Entity Workbench drag-and-drop with keyboard equivalents`

---

## Step 2.12 — Resource Introspection Proxy, SSRF filter, Canvas Panel, map widget

**Do:**
1. `lae/net/egress.py`: `EgressPolicy` loaded from `system_settings` (Step 2.17; until then from env `LAE_EGRESS_ALLOW`, `LAE_EGRESS_DENY`). `check(url)` resolves DNS, rejects RFC 1918, loopback, link-local, and `169.254.169.254`, applies deny then allow lists, and is applied on every redirect hop. All outbound `httpx` clients in `lae` must use it (add a test that greps for `httpx.AsyncClient(` outside `lae/net/`).
2. `POST /api/introspect {url}` (`edit_draft`): `HEAD` then bounded `GET` (2 MB, 10 s); classify as `iiif_manifest` (JSON with `@context` IIIF Presentation 2/3), `iiif_image` (`info.json`), `image` (`image/*`), `html` (scrape `<title>`, OpenGraph, embedded JSON-LD), `linked_data` (JSON-LD or RDF content type), `unknown`; return the Linked Art subgraph per spec section 6.4 as a JSON fragment plus `kind`.
3. Frontend: drop zone on the record header and on `entity_ref`/`representation` slots; `editor/DropDisambiguation.tsx` popover for header drops ("Add as representation" vs "Add as subject_of"); **Smart Paste** hooks the same endpoint when a URL or WKT is pasted into a text slot.
4. `frontend/src/widgets/IIIFViewer.tsx`: Canvas Panel web component (package confirmed in Spike S5) for images with region selection writing `SpecificResource` fragments, and for A/V manifests as far as Spike S5 showed Canvas Panel supports them. No other viewer (ADR-0012).
5. `frontend/src/widgets/WktMapEditor.tsx`: MapLibre GL JS + Terra Draw for `Place.defined_by`: point, bounding box, polygon with holes; normalize to lon/lat and counter-clockwise outer rings before writing `geo:wktLiteral`; raw WKT textarea and `.geojson` drop as the accessible equivalents; geometry stored as one LWW value. "Seed from gazetteer" searches TGN/GeoNames sidecars (Step 2.14) and copies their geometry.

**Verify:** `backend/tests/net/test_egress.py` covers private ranges and redirect hops; introspect tests for each kind with recorded fixtures; e2e drops an image URL and sees a VisualItem added.

**Commit:** `feat(net): SSRF egress policy` then `feat(editor): URL/IIIF introspection, Canvas Panel, WKT map editor`

---

## Step 2.13 — Single-record Mermaid diagram

**Do:**
1. `frontend/src/diagram/toMermaid.ts`: pure function `(jsonld, mode: "conceptual" | "full") -> string` emitting `graph LR`; aliases internal `/node/` IRIs as `n1, n2…`; conceptual mode hides `Name`/`Identifier` leaves. Unit tests on corpus records (snapshot).
2. `frontend/src/diagram/RecordDiagram.tsx`: renders with `mermaid`, re-renders on Y.Doc change (debounced 300 ms), click-to-scroll to the slot (`data-anchor-iri`), Copy Markdown and Download SVG buttons, `<title>`/`<desc>` in the SVG, and a text/table toggle.

**Verify:** `pnpm test --filter diagram`; e2e `axe` on the diagram drawer.

**Commit:** `feat(diagram): Mermaid record diagram`

---

## Step 2.14 — Sidecar Dataset Manager (migration 0014)

**Why:** Autocomplete and vocabularies must cover ULAN, TGN, and the Wikidata subset, not just AAT.

**Do:**
1. Migration `0014_sidecars.py`: `sidecar_datasets` with `id (text pk: aat, ulan, tgn, wikidata, geonames)`, `version (text)`, `graph_iri (text)`, `entity_count (bigint)`, `loaded_at`, `status (enum: loading, active, failed, unloaded)`, `manifest (JSONB)`.
2. `lae/sidecar/ingest.py`: `ingest(dataset_id, source_path, version)` runs the cascade: read Linked Art JSON-LD (JSONL), expand to N-Triples with `lae.rdf.expand_to_ntriples`, build into a staging graph `urn:graph:sidecar:<id>:<version>`, then in one Postgres transaction upsert `entity_labels` (`source = <id>`, labels from `identified_by` Names with `language` → BCP-47 via the Language `notation`, `default_label` from `_label`), swap the live graph IRI in `sidecar_datasets`, run `refresh_membership` for every `rule` vocabulary, and drop the previous graph. Expose `lae sidecar load <id> <path> --version`.
3. Source adapters in `lae/sidecar/sources/`:
   - `getty.py`: fetch records one at a time from `data.getty.edu` (already Linked Art; copy redirect/replacement handling from `lux/data-pipeline/lux_pipeline/sources/getty/fetcher.py`) and harvest changes from its ActivityStreams feed (`getty/harvester.py`); cache raw JSON on disk under `deploy/data/sidecars/<id>/`.
   - `wikidata.py`: read an existing LUX export (`lux export --type external --format lmdb` store, or `make_wikidata_lmdb.py` output) using a copy of `lux_pipeline/storage/idmap/lmdb.TabLmdb`; convert each cached record to JSONL. No dump streaming here.
   - `geonames.py`: `allCountries.txt` + `alternateNamesV2.txt` for BCP-47 labels (the LUX loader omits languages; add them).
4. Admin endpoints (`manage_system`): `GET /api/admin/sidecars`, `POST /api/admin/sidecars/{id}/load` (multipart or server path), `POST /api/admin/sidecars/{id}/unload`. Frontend `admin/SidecarsPage.tsx` shows version, count, timestamp, status, and buttons.
5. Autocomplete (`/api/search/autocomplete`) now unions sidecar graphs; results carry `source` and a disambiguating context line (lifespan for Person, parent for Place from `broader`/`part_of`).

**Verify:** load AAT, ULAN, TGN, and the Wikidata subset on the dev stack; `SELECT source, COUNT(*) FROM entity_labels GROUP BY source` shows all four; autocomplete for "Rembrandt" returns the ULAN record with lifespan; `refresh_membership("vocab:nationality")` returns > 0.

**Commit:** `db: sidecar_datasets (0014)` then `feat(sidecar): ingestion cascade and source adapters`

---

## Step 2.15 — Batch import CLI and Batch Staging Wizard

**Do:**
1. `lae import jsonld <path> --stage` and `lae import csv <path> --mapping <yaml> --stage`: write an `edit_batches` row (`kind = import`) whose manifest lists parsed records (stored in a new column `edit_batches.staged (JSONB)` or a side table `edit_batch_items` with `batch_id`, `seq`, `record (JSONB)`, `diagnostics (JSONB)`, `duplicate_of (text null)` — use the side table; add it in migration `0015_batch_items.py`). Validate every item with `lae.validation.validate`; run the duplicate check via autocomplete exact-label match per item.
2. CSV mapping YAML: `class`, `columns: {<csv header>: <profile slot id>}`; the importer uses the UI Profile slot `path` to place values, the same code the grid uses.
3. `POST /api/batches/{id}/commit` (from Step 2.8) handles `kind = import`: for new records mint UUIDv7 and publish via the Phase 1 publish service in chunks of 200; for updates (CSV with an `id` column) create forward versions.
4. `frontend/src/batch/StagingWizard.tsx` (route `/import`): upload → column mapping (dropdowns from profile slots) → validation summary (counts by severity, expandable rows) → duplicates (choose "skip", "create anyway", "link to existing") → commit with batch id shown and a Revert button afterwards.

**Verify:** import 1,000 fixture rows end-to-end; revert restores; bad rows surface diagnostics with codes, not prose.

**Commit:** `db: edit_batch_items (0015)` then `feat(batch): import CLI and Batch Staging Wizard`

---

## Step 2.16 — ActivityStreams change feed and Memento

**Do:**
1. `GET /activity/stream` → `OrderedCollection` with `first`/`last`; `GET /activity/stream/page/{n}` → `OrderedCollectionPage` of 100 activities built from `record_revisions` ordered by id: `Create` (version 1), `Update`, `Delete` (status deleted), `Move` (status merged, with `target`). Shape follows the IIIF Change Discovery API 1.0 (`@context`, `type`, `object {id, type}`, `endTime`). Cache pages except the last with `Cache-Control: max-age=3600`.
2. Memento on `/id/{uuid}`: honour `Accept-Datetime` by selecting the revision with the greatest `created_at <= datetime`, respond with `Memento-Datetime`, `Link: <…>; rel="original"`, `rel="timemap"`; `GET /id/{uuid}/timemap` returns a link-format TimeMap. `?version=N` already exists from Phase 1; make both share one resolver `lae/api/versions.py`.

**Verify:** `backend/tests/api/test_activity.py` walks the stream after create/update/merge/delete and asserts the four types in order; `test_memento.py` requests a datetime between two revisions and gets the earlier one.

**Commit:** `feat(interop): ActivityStreams change feed and Memento negotiation`

---

## Step 2.17 — System settings, health, metrics, maintenance mode (migration 0016)

**Do:**
1. Migration `0016_ops.py`: `system_settings` (`key (text pk)`, `value (JSONB)`, `version (int)`, `updated_by`, `updated_at`), `admin_audit_log` (`id (bigint pk)`, `actor_id`, `action (text)`, `target (text)`, `before (JSONB)`, `after (JSONB)`, `created_at`; no `UPDATE`/`DELETE` grants for the app role), `backup_jobs` (`id`, `kind (enum: scheduled, on_demand, verify)`, `status`, `started_at`, `finished_at`, `location (text)`, `checksum (text)`, `reason (text)`, `log (text)`).
2. `lae/settings_store.py`: typed accessors with defaults for `draft_ttl_days`, `qlever_rebuild_threshold`, `sparql_public_timeout_s`, `sparql_public_rate_limit`, `egress_allow`, `egress_deny`, `maintenance_mode (bool)`, `maintenance_banner (LangMap)`. Every write goes through `set(key, value, actor)` which bumps `version` and writes `admin_audit_log`. Replace the env fallbacks from Steps 2.12 and Phase 1 reaper with these accessors.
3. Extend the Phase 1 `GET /health/live` and `GET /health/ready` (add: QLever `ASK {}` within 2 s, outbox lag < 60 s, component statuses in the body), and add `GET /metrics` (Prometheus text via `prometheus-client`): `lae_outbox_pending`, `lae_outbox_failed`, `lae_sync_lag_seconds`, `lae_qlever_delta_triples` (scraped from QLever's own metrics), `lae_rooms_open`, `lae_ws_connections`, `lae_http_request_duration_seconds` histogram by route.
4. Maintenance mode: when `maintenance_mode` is true, `RoomManager` rejects new write updates with a typed WS message `{"type": "maintenance"}`, `POST …/publish` returns `503 {code: "maintenance"}`, and `/id/{uuid}` and `/api/search` keep working. add a `/ws/system` broadcast channel to `crdt-ws` (authenticated, read-only, JSON messages `{type, payload}`) and have `api` push `maintenance_banner` through the internal RPC `POST /internal/system/broadcast` so editors see a toast.
5. Public SPARQL: `POST/GET /sparql` read-only proxy to QLever enforcing `sparql_public_timeout_s`, rate limiting per IP, and rejecting any request body containing `INSERT`, `DELETE`, `LOAD`, `CLEAR`, `CREATE`, `DROP` as the first keyword after prefixes. QLever itself binds only to localhost / the internal network, so this proxy is the only public SPARQL entry point.
6. `frontend/src/admin/HealthDashboard.tsx` (route `/admin/health`, `manage_system`): panels for Postgres, QLever (delta triples vs threshold), sync worker (pending/failed with Retry and Skip buttons calling `POST /api/admin/outbox/{id}/retry|skip`), rooms, daemons (last run, next run, Run Now via `POST /api/admin/daemons/{name}/run`). `admin/SettingsPage.tsx` edits every setting with the version shown.

**Verify:** `curl localhost:8000/health/ready` returns `200` with component statuses; toggling maintenance mode blocks publish (`503`) and shows the banner in a second browser session; the public `/sparql` rejects an `INSERT DATA` body with `403`.

**Commit:** `db: system_settings, admin_audit_log, backup_jobs (0016)` then `feat(ops): health, metrics, settings, maintenance mode, public SPARQL proxy`

---

## Step 2.18 — Blue/green QLever rebuild automation

> **CRITICAL STEP — requires a larger model.** Implement and review this step with a model from the tier above Opus 5.5 (Claude Fable/Mythos class). Opus 5.5 may scaffold files and boilerplate, but the core logic, its tests, and the PR review must come from the larger model. Reason: watermark snapshot, replay, and atomic swap must be exactly right or the rebuilt index silently drops recent publishes.


**Why:** The delta-triple layer grows forever otherwise, and rebuilds must never take the public reader offline.

**Do:**
1. `lae/sync/rebuild.py`:
   - `snapshot_watermark() -> int`: in one transaction, record `max(outbox_events.id) where status = done` as `rebuild_watermark` in `sync_state`.
   - `export_ntriples(path)`: stream every `records` row with `status = active` to N-Triples (reuse the Phase 1 bulk exporter), plus every active sidecar graph, writing one file per graph.
   - `build_index(dir)`: run `qlever index` (qlever-control) into `deploy/data/qlever/<timestamp>/` with the Qleverfile; stream its output into `backup_jobs`-style log rows (new table not needed: reuse `admin_audit_log` `action = qlever.rebuild` with the log in `after`).
   - `replay(dir, from_id)`: start the new server on a second port with `--persist-updates`, apply every `outbox_events` row with `id > rebuild_watermark` using the normal diff path (`lae.sync.worker.apply_event` must accept a target URL).
   - `swap()`: update `sync_state.qlever_active_url` and the worker's target atomically; the API's QLever client reads the active URL per request; keep the previous server running for 10 minutes then stop it; keep its directory until the next successful rebuild.
2. Scheduler job `rebuild_if_needed` every 10 minutes: triggers when `lae_qlever_delta_triples > qlever_rebuild_threshold` and no rebuild is running (lock row in `sync_state`). Admin endpoint `POST /api/admin/qlever/rebuild` (`manage_system`) triggers on demand; the Health Dashboard shows progress and the last three rebuilds.
3. Consistency audit (scheduler, nightly): sample 500 records, compare `triples(last revision)` with `SELECT ?p ?o WHERE { <s> ?p ?o }` per owned subject, write mismatches to `qa_findings` (table arrives in Phase 3; until then, write to `admin_audit_log` `action = qlever.audit`).

**Verify:** on the dev stack with 1M records: publish continuously from a script during a rebuild; after swap, the published edits are present, `/id/{uuid}` and `/sparql` never returned non-200 during the rebuild (assert from the script log), and the delta-triple metric reset.

**Commit:** `feat(sync): blue/green QLever rebuild with watermark replay`

---

## Step 2.19 — Backups (pgBackRest)

**Do:**
1. Install pgBackRest natively (Homebrew `pgbackrest` on macOS, the PGDG `pgbackrest` package on Debian/Ubuntu; document both in `deploy/README.md`). Add `deploy/pgbackrest/pgbackrest.conf` and the PostgreSQL settings for WAL archiving (`archive_mode = on`, `archive_command = pgbackrest … archive-push`) as a documented config snippet. Repository on a local directory (S3 settings as commented alternatives).
2. Scheduler jobs: daily incremental, weekly full, both writing `backup_jobs` rows; weekly `verify` job that restores the latest backup into a scratch PostgreSQL cluster (`initdb` into a temporary data directory, started with `pg_ctl` on a spare port, removed afterwards) and runs `SELECT COUNT(*) FROM records` and a schema diff against `alembic heads`.
3. `POST /api/admin/backups` (`manage_backups`) with `{reason}` runs an on-demand backup; `GET /api/admin/backups` lists jobs; `frontend/src/admin/BackupsPage.tsx`.
4. `lae export loud --out <dir>`: full JSON-LD (gzipped JSONL) and N-Triples export with internal `/node/` IRIs stripped, for preservation downloads.

**Verify:** run the verify job locally; it reports success with matching counts. Delete a row in a scratch database, restore to a point before the delete, confirm the row is back.

**Commit:** `infra: pgBackRest PITR, backup jobs, LOUD export`

---

## Step 2.20 — Full-volume load and performance gate

**Do:**
1. Run the Phase 1 bulk bootstrap at 10M records (synthetic plus real) on a staging host; record timings in `docs/perf/phase-2-load.md`.
2. Run `scripts/perf/autocomplete_bench.py` (100 concurrent clients, 1,000 queries) and `scripts/perf/search_bench.py` against the loaded index; record p50/p95.
3. Trigger one full blue/green rebuild at this volume and record duration and peak disk.

**Verify:** autocomplete p95 < 100 ms, faceted search p95 < 500 ms, publish-to-QLever p95 < 1 s during the benchmark, zero non-200 public responses during the rebuild.

**Commit:** `test(perf): phase 2 full-volume benchmarks`

---

## Exit Checklist

- [ ] `lae vocab import` / `export` round-trip is clean; changing a Value Set updates pickers and validation without a deploy.
- [ ] Backlinks panel, delete protection (`409`), tombstones (`410`), merge (`301`) and unmerge verified by tests and by hand on two real records.
- [ ] Faceted search, grid view, fill-down, batch find-and-replace, and `Cmd+K` work with keyboard only and pass `axe`.
- [ ] Comments, watchlists, notifications, and the Review Inbox (Extend TTL / Inspect & Publish / Discard) work end to end.
- [ ] Entity Workbench drag-and-drop with "Link to slot…" keyboard equivalent; URL/IIIF drops through the egress-filtered proxy; Canvas Panel and WKT map editor in the profile.
- [ ] AAT, ULAN, TGN, and the Wikidata subset loaded as sidecars; autocomplete returns them with context lines.
- [ ] Batch Staging Wizard imports and reverts 1,000 rows.
- [ ] `/activity/stream` and Memento tests pass.
- [ ] `/health/ready`, `/metrics`, settings, maintenance mode, and the read-only public `/sparql` proxy are live.
- [ ] A blue/green rebuild completed at 10M records with zero public downtime; backups and the restore-verify job ran successfully.
- [ ] `docs/perf/phase-2-load.md` records the numbers above and all targets are met.
- [ ] The cataloging team has used the editor for live work for at least one week and their blocking issues are closed.

## Common Mistakes

- Querying QLever for backlinks right after a publish and getting a stale answer. The sync worker is asynchronous; tests must wait for the outbox row to be `done`.
- Copying the LUX `<scope>Words` SERVICE views for search. They are built at index time for the LUX schema and do not exist here. Use `ql:has-word` / `ql:contains-word` on the default graph.
- Letting the merge rewrite referrers outside the transaction that flips the source to `merged`. Everything in `execute_merge` is one transaction or it is wrong.
- Hiding unmatched `classified_as` values instead of showing them in Other Classifications. Lossless round-tripping is a hard requirement.
- Building any outbound HTTP call without `EgressPolicy`. The grep test in Step 2.12 exists to catch this.
- Running the rebuild against the live index directory. Blue/green always builds into a new directory and swaps.
- Writing English prose into `notifications.payload` or diagnostics. Store codes and parameters; the client localizes.
- Adding a physical-direction Tailwind class (`ml-`, `pr-`, `left-`). The Phase 0 CSS check will fail CI.
