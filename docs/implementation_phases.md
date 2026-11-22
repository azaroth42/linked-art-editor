# Implementation Phases: Collaborative Linked Art Editor

**Status:** Draft v0.1
**Companion to:** `spec/` (architecture; section numbers preserved from the original design document). This document is about *sequence*, not design: what gets built when, why in that order, and what has to be true before the next phase starts. Each phase has a step-by-step guide under `docs/phases/`; `docs/reuse_map.md` lists what to take from the adjacent repositories.

---

## Guiding Principles

1. **Retire the riskiest seams first.** Three things could invalidate the architecture: the JSON-LD ↔ Y.Doc projection, the revision-diff sync into QLever (updates, persistence, text-index freshness), and `pycrdt-websocket` under real load. All three are proven in Phase 0 before any UI is built on them.
2. **Vertical slices, not horizontal layers.** Each phase ends with something catalogers can use. Phase 1 is a complete, if narrow, edit → validate → publish → query loop for real records.
3. **Build the seams once.** The outbox, the projection layer, the room-manager interface, the UI Profile renderer, the Value Set registry, and the MCP tool surface are each built in the phase where they are first needed and then only extended.
4. **i18n and accessibility from the first commit.** The `no-literal-string` lint, `LangMap` config schema, CSS logical properties, pseudo-locale, and `axe-core` CI run from Phase 0. These cannot be retrofitted cheaply.
5. **Scale is tested with real volumes.** Every phase after Phase 0 runs its exit checks against a multi-million-record load, not a demo fixture.
6. **Agents come after the human loop is trusted.** MCP agents reuse the same rooms, validation, and publish gates. Those must be stable before non-human actors drive them.

Rough durations assume a core team of two to three engineers plus a domain lead. They are estimates for sequencing, not commitments.

---

## Phase 0 — Foundations & Risk Spikes (~4–6 weeks)

Guide: `phases/phase-0-foundations.md`

**Goal:** A buildable monorepo with CI, the core schema, and go/no-go answers on the load-bearing technology choices.

### Scaffolding
- Monorepo layout: `backend/` (Python 3.12, `uv`, FastAPI), `frontend/` (TypeScript, React 19, Vite, `pnpm`), `profiles/` (UI Profiles, SHACL shapes, Value Set YAML), `deploy/` (native PostgreSQL 16+ with `pgvector` and QLever setup, Procfile, systemd units; no Docker), `docs/`.
- CI from day one: `ruff`, `mypy`, `pytest`; `tsc`, `eslint` (including `eslint-plugin-i18next` no-literal-string), `vitest`, Playwright + `axe-core` smoke on a placeholder route; `LangMap` YAML schema check.
- Alembic migrations with the Phase 1 core tables: `records`, `record_revisions`, `entity_labels`, `crdt_drafts`, `crdt_updates`, `outbox_events`, `users`, `roles`, `api_tokens`.
- Golden corpus: every example from linked.art plus a sample of institutional records, stored as test fixtures.

### Spikes (each produces a short written result and a go/no-go)
| # | Spike | Question it answers | Fallback if "no" |
| :-- | :-- | :-- | :-- |
| S1 | **QLever updates** | `INSERT DATA` / `DELETE DATA` / `DELETE WHERE` on a 10M-triple index; `--persist-updates` survives restart; `ql:contains-word` prefix search finds literals inserted by Update; rebuild wall-clock at 100M and 1B triples; `SERVICE` to a stub endpoint. | Overlay recently published labels from `entity_labels` for autocomplete; defer `SERVICE` design (Open Question 1). |
| S2 | **`pycrdt-websocket` load** | 30 simulated editors in 10 rooms: awareness fan-out, memory per room, reconnect, update persistence latency. | Yjs-wire-compatible sidecar (y-websocket / Hocuspocus) behind the room-manager interface. |
| S3 | **Projection round-trip** | Golden corpus → Y.Doc → canonical JSON-LD is byte-identical after canonicalization, including array order via `_order`, unprofiled properties, and internal `/node/` IRI minting and stripping. | Redesign keyed-map rules before any UI depends on them. |
| S4 | **SHACL latency** | `pyshacl` per-record time on a draft `HumanMadeObject` shape; compiled evaluator alternatives. | Narrow SHACL to a small rule set; implement the rest as JSON-Schema or Python rules. |
| S5 | **Canvas Panel in React 19** | Web component embedding, Content State in/out, region selection, `sequence-panel` navigation, A/V playback. | No alternate viewer (ADR-0012): record gaps for the owner in `docs/questions.md`. |
| S6 | **Bulk bootstrap timing** | `COPY` + N-Triples export + `IndexBuilderMain` for 1M records; projected cost at 10M. | Tune expansion pipeline before Phase 1 load. |

**Exit criteria:** CI green on all gates; all six spike reports written; migrations apply to an empty database; the golden corpus loads and round-trips in S3.

---

## Phase 1 — Core Authoring Loop (~10–12 weeks)

Guide: `phases/phase-1-core-loop.md`

**Goal:** Two or three catalogers co-edit and publish real `HumanMadeObject`, `Person`, `Place`, and `Type` records, with every publish queryable in QLever within a second.

### Backend
- **Records & URIs:** UUIDv7 minting, `/id/{uuidv7}` content negotiation (JSON-LD with Linked Art profile, Turtle, HTML placeholder), `ETag`, versioned permalinks `?version=N`.
- **Stubs & transclusion:** Write-time normalization (retaining `_label` only for unresolved targets), batched locale-aware hydration from `entity_labels`.
- **CRDT room manager (`crdt-ws` process):** Lazy hydration from `records`, synchronous `crdt_updates` persistence, `draft_epoch` / `base_version`, awareness, internal RPC from `api` for validate and publish, idle eviction, 5-day reaper.
- **Projection & canonical triples:** Y.Doc → JSON-LD (sorted by `_order`), JSON-LD → canonical N-Triples via `pyoxigraph`, zstd blobs, internal `/node/` IRIs stripped on export, `/id/{uuid}/node/{id}` 303 redirect.
- **Validation v1:** Upstream Linked Art JSON-Schema in browser (Ajv) and backend; first project-authored SHACL shapes (date ordering, reference class ranges); structured diagnostic codes.
- **Publish transaction:** `records` + `record_revisions` + `entity_labels` + `outbox_events` in one transaction; bundled publish of dependent new stubs; multi-author attribution.
- **Sync worker:** `LISTEN/NOTIFY` with polling fallback, revision-diff `DELETE DATA` / `INSERT DATA`, `DELETE WHERE` path for drifted bulk-loaded records, micro-batching, `qlever_index_watermark`, dead-letter table.
- **Bulk bootstrap pipeline:** `COPY` + bulk N-Triples export + `IndexBuilderMain`; load an institutional sample plus AAT labels (seeded from the 59k-row `aat_dataset.csv` in `linkedart-parser`; the full AAT Linked Art sidecar arrives with the Phase 2 sidecar manager).
- **Auth & roles:** OIDC via Authlib, Personal Access Tokens, roles Viewer / Collaborator / Editor, `edit_draft` vs. `publish` gates.
- **Revisions:** History listing, typed semantic diff objects, 1-click revert.

### Frontend
- **UI Profile renderer:** YAML profile schema, path flattening, progressive disclosure, `Other Classifications` bucket, Raw JSON-LD Inspector drawer. `HumanMadeObject` first, then `Person`, `Place`, `Type`.
- **Core widgets:** Multilingual `Name` (BCP-47, per-field `dir`), EDTF date widget, entity-reference autocomplete from QLever (local + AAT sidecar) with inline stub creation, classification pickers backed by a hand-written Value Set bundle (the registry itself arrives in Phase 2).
- **Collaboration UI:** Yjs binding, presence, user-scoped `UndoManager`, validation drawer, Publish dialog with semantic diff and warning acknowledgment, Revision History Browser.
- **Public Reader v1:** Server-rendered read-only page at `/id/{uuid}` with JSON-LD/Turtle download and Canvas Panel for records with IIIF representations.
- **i18n & a11y scaffolding:** `react-i18next` + ICU, route-scoped namespaces, logical CSS properties, pseudo-locale toggle, keyboard-only cataloging smoke test in Playwright.

**Exit criteria:** Golden corpus round-trips through the full stack; 1M-record bulk load completed and timed; publish-to-QLever p95 under 1 s; three concurrent editors complete a scripted cataloging session without data loss; autocomplete p95 under 100 ms on the loaded index.

---

## Phase 2 — Curation, Vocabularies & Scale (~12–14 weeks)

Guide: `phases/phase-2-curation-vocab-scale.md`

**Goal:** The editor is the daily tool for a cataloging team working against the full record volume, with controlled vocabularies, backlinks, merges, search, and operational safety nets.

### Vocabulary Management Layer
- Tables `vocabularies`, `vocabulary_terms`, `slot_vocabulary_bindings`; enumerated sets shipped as an ETag-cached boot bundle; rule-backed sets resolved in QLever with the materialized membership cache.
- Slot bindings compile into JSON-Schema / SHACL enforcement levels; `classified_as` card partitioning driven by `metatype_uris` / `vocab_ids`.
- Vocabulary Manager admin UI (bindings, term ordering, scope notes, deprecation with usage counts), YAML import/export.

### Referential Integrity & Merge
- QLever-backed Backlinks panel grouped by predicate; delete protection; tombstones (`410`); manual merge with `merge_batches`, inbound-reference rewriting, `301`, and 1-click Unmerge; active-room deferral and ordered row locking.
- Undoable batch find-and-replace (shared by Vocabulary deprecation and the Batch Staging Wizard).

### Search, Sets & Daily Workflow
- Global Faceted Search & Browse Workbench (QLever full-text + facets); TanStack Table grid over `Set`s and result lists with fill-down and batch publish; `Cmd+K` palette.
- Anchored comments (`comment_threads` / `comments`), watchlists, notification inbox, Active Drafts & Review Inbox with Extend TTL / Inspect & Publish / Discard.
- Record cloning and starter templates; Entity Workbench with ontology-aware drag-and-drop and keyboard equivalents; Resource Introspection Proxy with SSRF egress filter for IIIF / image / web page / URI drops; MapLibre + Terra Draw for `Place.defined_by`.
- Single-record Mermaid diagramming (cheap, high value for debugging profiles).

### Sidecars, Bulk & Interop
- Sidecar Dataset Manager: ULAN, TGN, and the existing Wikidata subset ingested through the versioned cascade (QLever graph swap → `entity_labels` upsert → membership refresh).
- Batch import CLI and Interactive Batch Staging Wizard (CSV / JSON-LD dry run, mapping, validation preview, reversible batch id).
- ActivityStreams 2.0 `/activity/stream` aligned with IIIF Change Discovery; Memento `Accept-Datetime`.

### Operations Baseline
- Blue/green QLever rebuild automation driven by the delta-triple threshold; `/health/live`, `/health/ready`, `/metrics`; System Health Dashboard v1 (outbox depth, sync lag, rooms, dead letters with Retry / Skip).
- `pgBackRest` PITR and on-demand backups; Read-Only Maintenance Mode and broadcast banners; `system_settings` for TTL and thresholds.

**Exit criteria:** 10M-record load indexed and rebuilt blue/green with no public downtime; Value Set change propagates to pickers and validation without redeploy; merge and unmerge verified against backlink rewrites; faceted search and autocomplete p95 targets held at full volume; cataloging team using the editor for live work.

---

## Phase 3 — Semantic Search & AI Agents (~12–14 weeks)

Guide: `phases/phase-3-semantic-agents.md`

**Goal:** Hybrid semantic search, and AI agents working inside the same rooms and queues as humans under enforced governance.

### Semantic Layer
- Warm local embedding worker; `entity_embeddings` (`halfvec` HNSW) populated for concepts and reference authorities and kept transactional with `entity_labels`.
- Hybrid lexical + semantic autocomplete with RRF and the "Semantic match" badge; Pre-Creation Duplicate Guard.
- Resolve Open Question 1 (`/sparql/vector` `SERVICE` vs. application-layer fusion) using the S1 spike results and ship the chosen contract.

### MCP Server & Governance
- `/mcp` on MCP Python SDK v2: read-only resources, discovery tools, draft-authoring tools routed through `crdt-ws`, `validate_draft`, `preview_semantic_diff`, `add_comment`.
- Agent service accounts (Tier 1 / Tier 2 roles), `agent_audit_log`, `agent_quotas`, circuit breaker, Agent Operations Console.
- `agent_jobs` job model with `linkedart://job/{id}` progress resources and `cancel_job`.
- `curation_proposals` queue: Tier 1 `propose_merge` with negative cache; Tier 2 `claim_proposal`, `approve_merge` under server-enforced blast-radius guardrails, `reject_merge`, `escalate_to_human`.
- Visual Merge Workbench (dual-pane) as the human review queue with Approve / Reject / Unmerge.
- Scheduled QA Sweep & Link-Rot Checker producing `qa_findings`; Curation Campaign Queue Focus Mode over Sets, Smart Sets, and worklists.
- On-demand reconciliation via the new `lae-reconcile` process (`reconcile_entity`), vendoring the LUX pipeline's LMDB reconciler and name-variant extraction with network off and candidates returned with evidence.

### Embedded Agent Chat
- Multi-Provider LLM Adapter with envelope-encrypted `llm_provider_configs`, `local_only` / `allow_cloud` policy, optional per-user BYOK.
- In-process orchestrator as an MCP client; SSE streaming, context chips, confirmation cards, Revert Agent Turn, `user_perms ∩ agent_tier_perms`.

**Exit criteria:** Tier 1 proposals measured for precision on a labeled duplicate set; Tier 2 auto-merges stay within guardrails and every one is reversible; circuit breaker halts agents within one request; chat-driven edits appear live with distinct presence; hybrid autocomplete improves recall on a benchmark query set without regressing latency.

---

## Phase 4 — Explorers, Translation & Full Operations (~12–16 weeks)

Guide: `phases/phase-4-explorers-translation-admin.md`

**Goal:** Round out the macro-explorers, localization, structural workflows, and the complete Administration Control Plane.

### Explorers & Query
- Force-Directed Graph Explorer with supernode protection, drag-to-form, cluster-to-merge, path finder, live draft overlay; React Flow neighborhood mini-graph.
- Multi-Record Map Explorer; Chronological Timeline (single-record provenance and cross-record); Visual Query Builder with Dynamic Smart Sets; embedded YASGUI console wired into Grid / Map / Graph.

### Localization & Translation
- Track A: `la-i18n translate` CLI with ICU AST validation and provenance lockfile; first non-English locale pack; Vocabulary Manager "Translate Missing Labels".
- Track B: inline "Translate to..." into provenance-tracked sibling nodes; `translate_record_fields` on `agent_jobs`; staleness tracking via `source_content_hash`; ephemeral Public Reader translations.
- Two-speed local model routing and the interactive-first semaphore queue.

### Structural Workflows
- Entity Split Workbench with `split_batches` and Unsplit; Safe Record Reclassification Wizard; Inverse-Link Authoring in Backlinks; Smart Paste; composite creation flows; v1 Rebase Offline Changes Modal.

### Administration Control Plane (completion)
- Usage analytics (`usage_metrics_daily`) with export; user / role / token directory with session revocation and effective-permission view; central policy and SSRF allowlist editor; `admin_audit_log`; signed outbound webhooks; scheduled bulk LOUD exports; embedding model migration; protected-records and batch rollback console; restore verification runs.

### Hardening
- Full WCAG 2.2 AA audit across Public Reader, editor, and admin routes; performance pass on explorers at 5,000 nodes; chaos tests for sync worker and QLever restart recovery.

**Exit criteria:** All design-doc features either shipped or explicitly deferred with a reason; accessibility audit passes; disaster-recovery drill restores Postgres and rebuilds QLever within the stated objective.

---

## Reuse From Adjacent Repositories

See `reuse_map.md` for file-level detail. In summary: the golden corpus, pinned context, and JSON Schema come from `linked.art` and `json-validator` (Phase 0); the deterministic Linked Art builder and extraction schemas from `linkedart-parser` (Phases 3–4); the SPARQL term/builder modules, QLever client pattern, Qleverfile, vector `SERVICE` endpoint, and embedding encoder from `qlever-middletier` (Phases 1–3); the LMDB wrapper, name-variant reconciler, constrained union-find, per-class merge rules, and literal sanitation from `data-pipeline` (Phases 1–3); and the LiteLLM request/usage functions and JSON repair from `chai-engine` (Phase 3). Nothing is imported as a package; everything is vendored with a source header and tests.

## Cross-Phase Workstreams

| Workstream | Runs from | Notes |
| :-- | :-- | :-- |
| i18n & a11y gates | Phase 0 | Lint, pseudo-locale, `axe-core`, keyboard smoke tests on every PR. |
| Golden corpus & property tests | Phase 0 | Corpus grows with every new profiled class and widget. |
| Scale load tests | Phase 1 | 1M in Phase 1, 10M from Phase 2 onward, as CI-adjacent nightly jobs. |
| UI Profiles & SHACL shapes | Phase 1 | Profiles for `HumanMadeObject`, `Person`, `Place`, `Type` in Phase 1; `Group`, `VisualItem`, `LinguisticObject`, `Set`, `Activity`/`Event` in Phase 2. |
| Documentation | Phase 1 | Cataloger guide, admin runbook, MCP tool reference, each updated in the phase that ships the feature. |

---

## Deliberately Late

- **Agents (Phase 3):** They depend on trusted rooms, validation, and publish gates. Building them earlier would mean debugging two unknowns at once.
- **Force graph, timelines, query builder (Phase 4):** High visibility but no bearing on data integrity; they consume stable QLever and profile APIs.
- **Translation (Phase 4):** Requires the i18n foundations, the Value Set registry for glossary injection, and the `agent_jobs` model, all of which arrive earlier.
- **Rebase Offline Modal (Phase 4):** Rare path at 10–30 editors; the `draft_epoch` guard in Phase 1 already prevents corruption by rejecting stale replay.
- **Webhooks and analytics (Phase 4):** Pull-based `/activity/stream` from Phase 2 covers integration needs until then.

## Dependency Summary

```
Phase 0 spikes ──> Phase 1 core loop ──> Phase 2 vocab/merge/search/ops ──> Phase 3 semantic + agents ──> Phase 4 explorers/translation/admin
                     │                        │                                 │
                     └─ UI Profile renderer   └─ Value Set registry             └─ agent_jobs, MCP surface
                        projection layer         batch find/replace                 LLM adapter
                        outbox + sync worker     blue/green rebuilds
                        room-manager interface   Backlinks + merge
```
