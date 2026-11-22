# ADR-0009: Keep sidecar datasets as designed; no on-demand single-entity import in v1

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
The review proposed subsetting rules and an on-demand fetch of single external URIs because loading Wikidata wholesale is infeasible.

## Decision
Rejected for now: a good Wikidata subset and other large datasets already exist (LUX exports). Sidecars are loaded by the Phase 2 Sidecar Dataset Manager; no live per-entity fetch during editing.

## Consequences
References to URIs outside the loaded sidecars keep their `_label` (ADR-0006) and are flagged for QA.
