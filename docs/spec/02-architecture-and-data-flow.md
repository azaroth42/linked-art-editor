<!-- Section numbers are preserved from the original design document (now in docs/old/design_doc.md). Guides cite these numbers as 'spec §N.N'. Do not renumber. -->

## 2. High-Level Architecture & Data Flow

To prevent dual-write drift between PostgreSQL and QLever, all mutations commit atomically to PostgreSQL first. A transactional outbox relays committed changes to QLever asynchronously within sub-second latency.

```
+--------------------------------------------+   +------------------------------+
|         TypeScript + React 19 SPA          |   |      AI Agents (MCP)         |
| [UI Profile/D&D/Grid] [Yjs CRDT] [IIIF/Map]|   | [Tier 1 Triage / Tier 2 Rev] |
| [Public Reader]  [Force Graph]   [Mermaid] |   | [QA Worklists / Merge Rev]   |
| [i18n/ICU + RTL]  [WCAG 2.2 AA] [AI Chat]  |   | [Batch Translate / Chat Ops] |
| [Admin Control Plane] [Analytics / Health] |   | [Policy & Quota Governed]    |
| [Faceted Search/YASGUI] [Split/Queue/Hist] |   | [Split / Merge Proposals]    |
+---------------------+----------------------+   +---------------+--------------+
                      | REST / JSON-LD + Binary CRDT WS          | Streamable HTTP (/mcp)
                      +--------------------+---------------------+
                                           v
+-------------------------------------------------------------------------------+
|               Python API, CRDT & MCP Service (FastAPI + pycrdt)               |
| [OIDC / Token Auth]      [Tiered Role ACL]        [SHACL / JSON-Schema]       |
| [CRDT Room Manager]      [Transclusion]           [Vocab Registry & UI]       |
| [MCP Server (/mcp)]      [On-Demand Reconciler]   [URL/IIIF & 5d Reaper]      |
| [Proposal Queue & Guardrails] [Reversible Merge]  [Agent Ops / Kill Switch]   |
| [QA & Link-Rot Sweeper]       [Batch Staging UI]  [IIIF Change Discovery]     |
| [Local Embedder & LLM]   [Hybrid RRF Search]      [pgvector SPARQL SERVICE]   |
| [Agent Chat Orchestrator (In-Process MCP)]  [Multi-Provider LLM & Key Vault]  |
| [Admin Control Plane & Backups]  [Health / Metrics]  [SSRF Egress Filter]     |
| [Split/Reclassify/Memento] [Draft Epoch Guard]  [Signed Outbound Webhooks]    |
+------------------+-------------------------------+----------------------------+
                   |                               | Read-only SPARQL
                   | ACID Transactions             | (Autocomplete, Backlinks,
                   v                               |  Sidecar Vocabs, Search)
+----------------------------------+               v
|   PostgreSQL + pgvector (10M+)   |     +----------------------------------+
| - records (UUIDv7, JSONB LZ4)    |     |        QLever Triplestore        |
| - entity_labels (BCP-47 JSONB)   |     | - Flat default graph             |
| - entity_embeddings (pgvector)   |     | - Sidecar external vocab graphs  |
| - record_revisions (zstd N-T/LD) |     | - Full-text + GeoSPARQL          |
| - crdt_drafts (5d TTL, epoch)    |     | - Delta-triple edit sidecar      |
| - curation_proposals / batches   |     | - Vector SERVICE -> FastAPI/PG   |
| - watchlists/smart_sets/qa/trans |     +-----------------+----------------+
| - llm_configs / chat_sessions    |                       ^
| - admin_ops / metrics / backups  |                       | SPARQL 1.1 Update
| - outbox/webhooks/roles/vocabs   |                       | (Diff DELETE/INSERT)
+------------------+---------------+                       |
                   | LISTEN/NOTIFY                         |
                   v                                       |
+----------------------------------+                       |
|       Sync Worker (Python)       +-----------------------+
| - Revision-diff N-Triples        |
| - Micro-batching (100-500ms)     |
| - Idle+Threshold Offline Rebuild |
| - Signed Outbound Webhooks       |
+----------------------------------+
```
