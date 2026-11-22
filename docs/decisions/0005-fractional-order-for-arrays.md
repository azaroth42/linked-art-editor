# ADR-0005: Fractional-index _order on keyed CRDT maps to preserve array order

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
Keyed Y.Maps deduplicate and merge cleanly but discard source order, which matters for `Name.part`, display order, and verbatim round-tripping of unprofiled arrays.

## Decision
Every keyed entry stores a float `_order` (initial index, midpoints on insert); projection sorts by `_order` then key and drops the field.

## Consequences
Both the Python and TypeScript ops must use identical arithmetic and shared fixtures (Phase 1 Steps 1.4 and 1.17).
