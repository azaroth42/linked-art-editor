# ADR-0007: DELETE WHERE fallback for first edits of bulk-loaded records

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
Bulk-loaded records have no stored N-Triples; if the context or expander changed since the index build, a computed diff would miss triples and leave orphans.

## Decision
Record the bootstrap context and expander versions; when they differ at first edit, the sync worker issues `DELETE WHERE { <s> ?p ?o }` over owned subjects, then `INSERT DATA`. A sampled Postgres-vs-QLever audit runs nightly.

## Consequences
Outbox payload carries `drift_check`; Phase 1 Steps 1.10–1.11; audit in Phase 2 Step 2.18 / Phase 3 Step 3.10.
