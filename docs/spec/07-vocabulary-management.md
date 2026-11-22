<!-- Section numbers are preserved from the original design document (now in docs/old/design_doc.md). Guides cite these numbers as 'spec §N.N'. Do not renumber. -->

## 7. Vocabulary Management Layer

In Linked Art, most domain distinctions live inside `classified_as` targeting `Type` (and its subclasses `Material`, `Language`, `MeasurementUnit`, `Currency`) rather than separate ontology classes. A dedicated **Vocabulary Management Layer**—functioning as a lightweight, Linked Art–native equivalent of Arches' Reference Data Manager (RDM)—provides a single source of truth governing allowed concept lists across UI Profiles, SHACL validation, drag-and-drop targeting, and MCP agent grounding.

### 7.1 Two-Tier Value Set Model

* **Enumerated / Curated Value Sets (Small, Ordered Lists):** Explicitly ordered lists of `Type` URIs with optional default values and cataloger scope notes (e.g., `vocab:person_name_parts` for Given/Middle/Family/Prefix/Suffix; `vocab:work_title_types` for Primary Name/Title/Subtitle/Alternate Title/Translated Title; plus Dimension types, Measurement Units, Statement types, and Identifier types). Shipped as an ETag-cached JSON bundle to the React SPA at boot so dropdowns, radio pills, and segmented toggles render with zero SPARQL latency.  
* **Rule-Backed / Dynamic Value Sets (Large, Hierarchical, or Metatype-Scoped):** Declaratively defined over local records and locally loaded Linked Art sidecar datasets in QLever (`urn:graph:sidecar:<dataset>`) using Linked Art metatype membership (e.g., concepts classified as AAT *Occupation*, *Gender*, *Nationality*, *Type of Work*, or *Style*), `broader+` hierarchy roots with optional exclusions, or concept class constraints (`Material`, `Language`, `MeasurementUnit`, `Currency`, `Type`). Supports an optional pinned shortlist of frequently used terms on input focus while typing queries the full scoped tree in QLever.

### 7.2 Contextual Slot Binding & Metatype Routing

* **Class- and Path-Scoped Bindings:** Value Sets bind to specific `(EntityClass, PropertyPath)` slots so `Person` records expose Name Part types (`identified_by[Name].part[Name].classified_as`), Gender, Occupation, and Nationality, while `HumanMadeObject`, `VisualItem`, and `LinguisticObject` records expose Work Title types, Type of Work, Style, and Shape—preventing cross-class concept leakage.  
* **Bidirectional Routing & Lossless Fallback:** On load, the UI Profile reads `metatype_uris` / `vocab_ids` from `entity_labels` to route each `classified_as` URI into its matching card, placing any unmatched valid `Type` into a read/removable **"Other Classifications"** fallback bucket to preserve lossless round-tripping.  
* **Linked Art Metatype Integrity:** Adding a local or sidecar concept to a metatype-backed Value Set ensures the concept carries the corresponding Linked Art metatype URI so downstream discovery systems (such as LUX) interpret it consistently.

### 7.3 Single Source of Truth Across Subsystems

* **UI Widgets:** Drives dropdowns, multi-select comboboxes, optional hierarchical tree pickers for AAT facets, and inline cataloger scope notes.  
* **SHACL & JSON-Schema Validation:** Compiles slot bindings directly into `strict` (`sh:Violation`), `extensible` (`sh:Warning`), or `advisory` validation rules.  
* **Concept-Aware Drag-and-Drop:** Refines drop-target highlighting to Value Set / metatype granularity rather than coarse `Type` class matching.  
* **MCP AI Agent Grounding:** Exposes active Value Sets and slot bindings as cacheable MCP resources so agents know exact allowed URIs per class and slot without hallucinating vocabulary terms.

### 7.4 Vocabulary Manager Admin UI & Term Lifecycle

* **Slot & Value Set Workspace:** Dedicated admin UI (Editor / Vocabulary Admin role) organized by Linked Art class to configure slot bindings, toggle curated vs. rule-backed modes, set enforcement levels, edit scope notes, and drag-to-reorder terms without code changes or container redeploys. Labels and scope notes include a compact BCP-47 `LangMap` pill switcher with sidecar-first translation resolution and a 1-click **"Translate Missing Labels & Scope Notes"** local LLM button (marking generated entries as auto-translated until confirmed by a Vocabulary Admin).  
* **Unified Local + Sidecar Curation:** Search across local records and sidecar datasets to include existing authority URIs, or mint new local `Type` records (stored in `records` and `entity_labels` with `broader` AAT parent links and optional `equivalent` links) so institutional house terms behave identically to external authority terms.  
* **Deprecation & One-Click Migration:** Marking a concept deprecated hides it from pickers for new edits while preserving display on existing records. QLever backlinks surface exact usage counts (e.g., *"Used in 184 records"*) alongside a 1-click **"Replace Across Records"** action invoking the undoable batch find-and-replace workflow.  
* **Versionable Config Export/Import:** Value Set definitions and slot bindings export and import as declarative YAML/JSON so institutions can version-control vocabulary policy in Git or share a Linked Art Core Vocabulary starter pack.
