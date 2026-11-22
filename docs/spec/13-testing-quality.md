<!-- Section numbers are preserved from the original design document (now in docs/old/design_doc.md). Guides cite these numbers as 'spec §N.N'. Do not renumber. -->

## 13. Testing & Quality Strategy

* **Golden Round-Trip Corpus:** The Linked Art example records (plus institutional samples covering every profiled class and unprofiled extensions) load -> hydrate into a Y.Doc -> project -> strip, and must reproduce the input JSON-LD byte-for-byte after canonicalization, including array order.  
* **CRDT Convergence Property Tests:** Randomized concurrent operation sequences across multiple simulated clients (keyed-map inserts, `_order` moves, Y.Text edits, offline replay against bumped `draft_epoch`) must converge to identical projections.  
* **Sync Worker Idempotency & Consistency:** Replaying any outbox range (including duplicates and crash mid-batch) must yield the same QLever state; fixtures pin canonical N-Triples serialization and diff output; the sampled PostgreSQL-vs-QLever audit runs in CI against a small index.  
* **Validation Fixtures & Latency Budgets:** Per-class JSON-Schema and SHACL fixtures (valid, `sh:Warning`, `sh:Violation`), with CI asserting the per-record validation latency budget that batch staging and agent loops depend on.  
* **Existing Gates:** `eslint-plugin-i18next` no-literal-string lint, `LangMap` schema checks, pseudo-locale rendering, and `axe-core` + Playwright accessibility and keyboard-only smoke tests (Sections 6.7 and 6.9).
