# Glossary

Terms are grouped by origin. Project-specific terms are marked **[LAE]**.

## The project

- **Laertes [LAE]:** this project: the *Linked Art Editor with Real Time Entity Synchronization*. The `lae` prefix of the package, CLI, and `LAE_` settings is the first three letters of the name.

## Linked Art and CIDOC-CRM

- **Linked Art:** A JSON-LD profile of CIDOC-CRM for cultural heritage data. Records are JSON documents with `@context`, `id`, `type`, and property arrays. Model docs: `/Users/rs2668/Development/linked-art/linked.art/docs/model/`.
- **Record:** One top-level Linked Art entity with its own URI, e.g. a `HumanMadeObject`, `Person`, `Group`, `Place`, `Type`, `VisualItem`, `LinguisticObject`, `DigitalObject`, `Set`, `Activity`, `Event`.
- **Embedded node:** A nested object inside a record with no public URI of its own, e.g. `Name`, `Identifier`, `Dimension`, `TimeSpan`, `Production`, `AttributeAssignment`. In public JSON-LD these have no `id`.
- **Reference / stub:** A nested object that points at another record: `{"id": "...", "type": "Person", "_label": "..."}`. Only `id` and `type` are owned by the referencing record; `_label` is a convenience copy.
- **`classified_as`:** The property carrying `Type` concepts (usually Getty AAT URIs) that refine meaning, e.g. a `Name` classified as *Primary Name*.
- **Metatype:** A `classified_as` on a `Type` itself, saying what kind of concept it is (Nationality, Occupation, Style, Gender). Linked Art uses metatypes to route concepts into the right slot.
- **AAT / ULAN / TGN:** Getty vocabularies for concepts, people and organizations, and places. `http://vocab.getty.edu/aat/300404670` is the AAT URI for *Primary Name*.
- **`equivalent`:** Links a record to the same entity in another dataset (Wikidata, ULAN, VIAF).
- **EDTF:** Extended Date/Time Format, e.g. `1880~/1895?`. Parsed into `TimeSpan` bounds.
- **`@context`:** The JSON-LD context that maps Linked Art keys to CRM IRIs. Pinned per version in `profiles/context/`.

## RDF, SPARQL, QLever

- **Triple:** Subject, predicate, object. A record expands to a few dozen to a few hundred triples.
- **N-Triples:** One triple per line, no prefixes. The project's canonical triple serialization: sorted, deduplicated lines.
- **Blank node:** An RDF node without an IRI. The project avoids them by giving embedded nodes internal IRIs (see *skolemized node IRI*).
- **SPARQL Update:** `INSERT DATA`, `DELETE DATA`, `DELETE WHERE` statements that modify a triplestore.
- **QLever:** The SPARQL engine used as the derived search and graph index. It is never the system of record.
- **Delta triples:** QLever's in-memory record of triples added or removed since the index was built. Persisted with `--persist-updates`.
- **Text index / `ql:contains-word` / `ql:has-word`:** QLever's full-text facilities. The text index is built at index time; freshness after updates is verified in Spike S1.
- **SERVICE:** SPARQL federation keyword. QLever can call an external SPARQL endpoint inside a query; the project uses this for vector search.
- **Blue/green rebuild [LAE]:** Building a new QLever index beside the live one from a Postgres snapshot, replaying outbox events past the snapshot watermark, then switching traffic.

## CRDT and collaboration

- **CRDT:** Conflict-free replicated data type. Concurrent edits merge automatically without a central lock.
- **Yjs / pycrdt:** The JavaScript and Python CRDT libraries used. Wire-compatible over `y-websocket` / `pycrdt-websocket`.
- **Y.Doc, Y.Map, Y.Text:** The shared document and its map and text types.
- **Room [LAE]:** One live Y.Doc per record being edited, hosted by the `crdt-ws` process.
- **Draft [LAE]:** The unpublished state of a record, i.e. the room's Y.Doc persisted in `crdt_drafts` / `crdt_updates`. Never in QLever.
- **Awareness:** Yjs presence channel: who is in the room, cursors, agent badges.
- **`draft_epoch` [LAE]:** Counter bumped each time a draft is cleared by a publish. Clients holding offline edits against an old epoch are rejected and shown the rebase modal.
- **`_order` [LAE]:** Fractional-index value stored on every entry of a keyed map so arrays round-trip in their original order.
- **Projection [LAE]:** Converting a Y.Doc to canonical Linked Art JSON-LD (`project`) and back (`hydrate`).

## Project data model [LAE]

- **System of record:** PostgreSQL. `records` holds published JSON-LD; `record_revisions` holds every published version as zstd N-Triples and zstd JSON-LD.
- **Skolemized node IRI / internal node IRI:** `<record_uri>/node/<uuidv7>` assigned to every embedded node so triples can be diffed and comments anchored. Stripped from public JSON-LD; visible in SPARQL.
- **Owned subject:** The record IRI and its internal node IRIs. A record may only emit triples whose subject it owns.
- **Transclusion:** Filling `_label` on references at read time from `entity_labels` instead of storing labels.
- **`entity_labels`:** Postgres lookup table of every known entity URI (local and sidecar) with BCP-47 labels, default label, metatypes, and Value Set ids.
- **Sidecar dataset:** An external vocabulary or authority loaded locally as Linked Art (AAT, ULAN, TGN, Wikidata subset) into its own QLever graph `urn:graph:sidecar:<name>` and into `entity_labels`.
- **Outbox:** `outbox_events` rows written in the same transaction as a publish; the sync worker turns them into QLever updates.
- **Watermark:** The id of the last outbox event applied to the live QLever index (`sync_state.qlever_index_watermark`). Its companion `sync_state.qlever_index_watermark_time` is when the Postgres snapshot the live index was built from was taken (ISO 8601; written by `lae bootstrap build-index`, and by every rebuild that swaps in a new index). QLever's text index never sees literals added by updates (Spike S1), so autocomplete also searches the `entity_labels` rows updated after that time (the *overlay*, Step 1.13).
- **Tombstone:** A deleted record kept with `status = 'deleted'`; its URI returns `410 Gone`.
- **Merge batch / split batch:** Manifests allowing one-click undo of a merge or split.
- **UUIDv7:** Time-ordered UUID used for every record and node id.

## Vocabularies and UI [LAE]

- **UI Profile:** YAML per Linked Art class describing sections, slots, widgets, and the JSON-LD path each slot edits. In `profiles/ui/`.
- **Slot:** One editable field in a profile, bound to a path and optionally a Value Set.
- **Value Set:** A named list or rule defining allowed `Type` URIs for a slot. *Enumerated* sets are small ordered lists; *rule-backed* sets are defined by metatype or hierarchy and resolved in QLever.
- **Enforcement level:** `strict` (violation blocks publish), `extensible` (warning), `advisory` (UI only).
- **Other Classifications:** The fallback bucket for `classified_as` values no Value Set claims. Guarantees nothing is lost.
- **LangMap:** `{"en": "...", "fr": "..."}` object used for every human-readable config string.
- **Diagnostics:** Validation results as `{code, path, severity, params}` so the client localizes messages.

## Agents and governance [LAE]

- **MCP:** Model Context Protocol. The editor exposes resources and tools at `/mcp`.
- **Tier 1 agent:** Cheap discovery model with `edit_draft` and `propose_merge` only.
- **Tier 2 agent:** Frontier model with `review_proposal` and guarded `approve_merge_scoped`.
- **Curation proposal:** A queued cross-record suggestion (merge, split, reclassify) with evidence and a status lifecycle.
- **Negative cache:** Record pairs marked "do not merge" so agents stop re-proposing them.
- **Blast radius:** The guardrails (protected flag, backlink count, external id match, contradictions) deciding whether a Tier 2 agent may auto-execute a merge.
- **`agent_jobs`:** Server-owned table for long-running agent operations; replaces the MCP Tasks extension.
- **Circuit breaker:** The admin switch that pauses all agents or just auto-merges.
