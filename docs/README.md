# Collaborative Linked Art Editor — Documentation Index and Status Board

**Start here.** This file tells an implementing agent what exists, what has been decided, what has been done, and where to begin. Keep the Status section current: every time a guide step is completed, tick it here in the same PR.

## 1. Current status

| Item | State |
| :-- | :-- |
| Repository | Initial commit only: `LICENSE`, `README.md`, `.gitignore`, and this `docs/` tree. **No application code exists yet.** |
| Design | Complete and locked. See `spec/` and `decisions/REGISTER.md`. |
| Implementation plan | Complete. Five phases with step-by-step guides under `phases/`. |
| Spikes | None run yet. Phase 0 runs them. |
| Next action | **Phase 0, Step 0.1** in `phases/phase-0-foundations.md` (GitHub issue #1). |
| GitHub issues | One issue per guide step plus one tracking issue per phase, created in dependency order with labels `phase-N`, `difficulty:S/M/L/XL`, `area:*`, and `critical-model`. Tracking issues: Phase 0 #13, Phase 1 #36, Phase 2 #57, Phase 3 #71, Phase 4 #91. Each step issue lists its dependencies; work them in issue-number order within a phase. |

### Progress board

Tick a step only when its **Verify** block passes, CI is green, and the commit exists. Add the PR number, and close the step's GitHub issue in the same PR (`Closes #N`).

| Phase | Steps done | Notes |
| :-- | :-- | :-- |
| 0 — Foundations & spikes | 0 / 12 | |
| 1 — Core authoring loop | 0 / 22 | |
| 2 — Curation, vocabularies, scale | 0 / 20 | |
| 3 — Semantic search & agents | 0 / 13 | |
| 4 — Explorers, translation, admin | 0 / 19 | |

Detailed per-step checkboxes live at the bottom of each phase guide (Exit Checklist) and in each step's **Verify**.

## 2. Reading order for a new agent

1. `README.md` (this file).
2. `glossary.md` — vocabulary used everywhere else.
3. `conventions.md` — layout, naming, testing, commits, the model-tier policy (§10), and what to do when stuck (§8).
4. `decisions/REGISTER.md` — every decision in one table. If you are about to choose between options, check here first; the choice has probably been made.
5. `implementation_phases.md` — the roadmap and why the order is what it is.
6. The guide for the phase you are working on, under `phases/`. Read its "Read first" line; it names the `spec/` sections that are the specification for that phase.
7. `reuse_map.md` when a step says to vendor or copy from an adjacent repository.

## 3. Document map

| Path | What it is |
| :-- | :-- |
| `spec/README.md` and `spec/*.md` | The design, split by topic. Section numbers (§) are preserved from the original design document; guides cite them as "spec §3.2". |
| `spec/api-index.md` | Generated index of every endpoint, WebSocket route, MCP tool, and CLI command with the guide step that builds it. |
| `decisions/REGISTER.md` | Consolidated decision register (architecture, editing, validation, UI, security, tooling). |
| `decisions/NNNN-*.md` | Architecture Decision Records. `0001`–`0015` are Accepted; `0016` is Proposed and is finalized in Phase 3 Step 3.3. |
| `implementation_phases.md` | Roadmap: phases, exit criteria, cross-phase workstreams, what is deliberately late. |
| `phases/phase-0-foundations.md` | Monorepo, CI, schema, golden corpus, six spikes. |
| `phases/phase-1-core-loop.md` | Records, RDF layer, projection, labels, API, auth, CRDT rooms, validation, publish, sync, bootstrap, autocomplete, revisions, reaper, frontend foundation, editor, reader, scale gate. |
| `phases/phase-2-curation-vocab-scale.md` | Vocabulary registry and manager, backlinks, merge/unmerge, search, grid, batch, comments/inbox, workbench, introspection proxy, Mermaid, sidecars, import wizard, activity stream, ops baseline, blue/green rebuilds, backups, 10M gate. |
| `phases/phase-3-semantic-agents.md` | Embeddings, hybrid search, vector SERVICE, agent governance, MCP server and tools, jobs, proposals and merge review, sweeps, QA, reconcile service, LLM adapter, chat. |
| `phases/phase-4-explorers-translation-admin.md` | ResultSet contract, graph/map/timeline explorers, query builder, YASGUI, LLM routing, i18n pipeline, data translation, split, reclassify, inverse links, smart paste, rebase modal, admin control plane, webhooks, exports, hardening. |
| `glossary.md` | Terms. |
| `conventions.md` | Engineering rules and the model-tier policy. |
| `reuse_map.md` | What to take from the adjacent repositories, file by file. |
| `spikes/` | Created in Phase 0. One report per spike with GO / NO-GO. |
| `questions.md` | Created on first use. Questions for humans, with the step number. |
| `old/` | Superseded documents. Never implement from them. |

## 4. Rules that override everything else

- **Build what the spec says.** If the spec is silent, choose the simplest lossless option and write an ADR. If the spec looks wrong, write a *proposed* ADR and a question; do not edit the spec and do not build the alternative.
- **Critical steps need the larger model** (conventions §10). The banner in the guide is authoritative.
- **Round-tripping is a hard gate.** `tests/test_roundtrip.py` over the whole corpus must pass before any PR that touches `lae.projection`, `lae.rdf`, or the frontend path ops merges.
- **Never import adjacent repositories as packages.** Vendor files with a source header (conventions §8).
- **Never modify the adjacent repositories.** They are read-only inputs listed below.
- **Update this file's progress board** in the same PR that completes a step.

## 5. Adjacent repositories (read-only)

| Path | Contents |
| :-- | :-- |
| `/Users/rs2668/Development/linked-art/linked.art` | Linked Art model documentation; the 211 example records under `docs/example/`; the `@context` under `docs/ns/v1/`. |
| `/Users/rs2668/Development/linked-art/json-validator` | Linked Art JSON Schema (Draft 7) under `schema/`. |
| `/Users/rs2668/Development/linked-art/linkedart-parser` | Natural-language-to-Linked-Art service: deterministic builder, extraction schemas, reconciliation pattern, local AAT CSV. |
| `/Users/rs2668/Development/lux/data-pipeline` | LUX ingestion pipeline: source loaders, LMDB reconciliation indexes, identity resolution, merge rules. |
| `/Users/rs2668/Development/lux/qlever-middletier` | LUX QLever middle tier: SPARQL client and builder, Qleverfile, vector SPARQL SERVICE sidecar, embedding code. |
| `/Users/rs2668/Development/lux/qlever-dev` | QLever source (upstream master). The binary the project builds and pins. |
| `/Users/rs2668/Development/llm/chai-engine` | LLM workflow engine: LiteLLM request handling, usage accounting, JSON repair, append-only correction rows. |

Local tooling already installed on the development machine: Postgres.app (with `vector` and `pg_trgm`), Homebrew `qlever` and `qlever-control` 0.6.0. See Phase 0 Step 0.3 for the native setup.
