<!-- Section numbers are preserved from the original design document (now in docs/old/design_doc.md). Guides cite these numbers as 'spec §N.N'. Do not renumber. -->

## 1. Vision & Design Philosophy

This system combines the strongest ergonomics of three established platforms for cultural heritage data authoring:

* **Arches:** Schema-aware forms ("cards") that hide CIDOC-CRM graph complexity behind intuitive, domain-specific inputs.  
* **Wikidata:** Collaborative editing, transparent revision history, statement-level provenance, and seamless authority linking.  
* **Google Docs:** Frictionless autosaving, real-time awareness of co-editors, inline discussion, and zero fear of lost work.

### Core Engineering Principles

1. **Single System of Record:** PostgreSQL owns durability, versioning, drafts, and permissions. QLever is a derived, queryable graph index—never the sole source of truth.  
2. **Draft/Publish Isolation:** Continuous autosaving must never destabilize the triplestore or leak broken fragments into search and autocomplete.  
3. **Lossless Round-Tripping:** Valid Linked Art JSON-LD that the UI does not explicitly model must pass through edit cycles untouched.  
4. **Cohesive Ontology Over Raw Forms:** The UI adapts to Linked Art patterns (flattening multi-hop CRM structures) rather than exposing raw RDF triples to catalogers.  
5. **Open Once Published & Single-Tenant Simplicity:** This is a Linked Open Usable Data (LOUD) authoring platform, not a closed collections management system. All published records are fully open with no field-level redaction, and each installation serves a single tenant cleanly.  
6. **Human + AI Co-Authoring & Tiered Governance via MCP:** AI agents collaborate through the same live CRDT draft rooms, validation rules, and structured curation queues as humans. Cheap discovery agents edit drafts and propose cross-record merges, frontier-model arbiter agents evaluate proposals (auto-executing low-blast-radius merges within strict guardrails, rejecting false positives, or escalating uncertain/high-impact cases to human Editors), and human Editors retain final oversight and 1-click reversibility.
