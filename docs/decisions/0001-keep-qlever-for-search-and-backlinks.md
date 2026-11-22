# ADR-0001: Keep QLever as the engine for autocomplete, backlinks, and reference discovery

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
The review proposed a Postgres `record_refs` edge table and Postgres trigram autocomplete because QLever is eventually consistent and its text index is built at index time.

## Decision
Rejected. QLever remains the search and graph engine so autocomplete can be graph-aware. Backlinks, delete protection, merge referrer discovery, and the Tier 2 backlink guardrail query QLever. Tests must wait for the outbox event to be `done` before asserting on QLever.

## Consequences
Spike S1 must prove text-index freshness after `INSERT DATA`; if it fails, autocomplete overlays recently published rows from `entity_labels` until the next rebuild (Phase 1 Step 1.13). No `record_refs` table exists.
