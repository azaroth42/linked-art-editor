# Specification (split from the original design document)

These files are the authoritative design. Section numbers (§) are preserved from the original single-file design document so that every guide reference such as "spec §3.2" resolves here. Do not renumber sections. If a decision changes, write an ADR in `docs/decisions/` and then edit the relevant section with a dated note.

| File | Sections | Topic |
| :-- | :-- | :-- |
| `01-vision-and-principles.md` | 1 | Vision, core engineering principles |
| `02-architecture-and-data-flow.md` | 2 | High-level architecture, data flow diagram |
| `03-data-model-identifiers-graph.md` | 3.1–3.4 | PostgreSQL tables, stubs and transclusion, flat graph model, skolemized node IRIs, revision-diff sync, blue/green rebuilds, merges, splits, reclassification |
| `04-editing-lifecycle-crdt-publishing.md` | 4.1–4.3 | Draft/publish boundary, CRDT schema and `_order`, process topology, epoch guard, diffs, review workflows |
| `05-validation.md` | 5 | JSON-Schema, SHACL, rules, severities, Value Set enforcement, QA sweeps |
| `06-ui-workbench.md` | 6.1–6.6 | UI Profiles, widgets, autocomplete, drag-and-drop, power-user workbench, explorers and Public Reader |
| `06.7-i18n-and-translation.md` | 6.7 | Four-layer i18n, local LLM translation tracks |
| `06.8-agent-chat.md` | 6.8 | Embedded Agent Chat Workbench |
| `06.9-accessibility.md` | 6.9 | WCAG 2.2 AA requirements |
| `07-vocabulary-management.md` | 7.1–7.4 | Value Sets, slot bindings, Vocabulary Manager |
| `08-security-auth-governance.md` | 8.1–8.3 | Authentication, permissions, tiers, public QLever access |
| `09-ai-agents-mcp.md` | 9 | MCP server surface, tools, proposals, LLM adapter, agent observability |
| `10-administration-operations.md` | 10.1–10.5 | Backups, health, analytics, users and tokens, policy and SSRF, sidecars, maintenance |
| `11-technology-stack.md` | 11 | Stack table |
| `12-bulk-operations-interoperability.md` | 12 | Content negotiation, ActivityStreams, Memento, batch import/export |
| `13-testing-quality.md` | 13 | Testing strategy |
| `14-locked-decisions-artifacts-open-questions.md` | 14 | Locked decisions table, next design artifacts, open questions and spikes |
| `api-index.md` | — | Every endpoint, WebSocket, MCP tool and CLI command, with the guide step that builds it (generated) |

See also `docs/decisions/REGISTER.md` for the consolidated list of every decision, including the ones made during the November 2026 review and the tooling choices fixed in the phase guides.
