# ADR-0010: Drafts are not indexed or searchable

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
The review proposed a draft overlay in `entity_labels` so unpublished stubs could be found and transcluded.

## Decision
Rejected: drafts stay out of QLever and `entity_labels`; the draft/QA status facet is computed from Postgres after the QLever page; unpublished stubs show their draft label inside the room only.

## Consequences
Phase 2 Step 2.7 applies the draft facet post-query.
